"""Writer: allowlist, ``--out`` check, staging, atomic swap, the
``publish.json`` index and the ``publish_run`` orchestration."""

import re
import shutil
import tempfile
from pathlib import Path

from smol_frontier import __version__, paths, series
from smol_frontier.classify.display import write_display_png
from smol_frontier.classify.geotiff import write_published_geotiff
from smol_frontier.publish.documents import (
    SCHEMA_VERSION,
    canonical_json,
    latest_document,
    latest_record,
    series_document,
    validate,
)
from smol_frontier.publish.errors import PublishRefused, _require_dict
from smol_frontier.publish.map import (
    _MAP_DIR,
    PublishedMap,
    build_map,
    map_path,
    png_path,
)
from smol_frontier.publish.select import _read_manifest, select_run

# WHY: allowlist — the only files `publish` may ever write, so nothing
#   unexpected (e.g. source imagery) can reach the published directory.
ALLOWED_FILES = frozenset({"latest.json", "series.json", "publish.json"})
# WHY: the integrity index is written last, over the other staged files; it
#   is the only file that never lists itself.
INDEX_FILE = "publish.json"

# WHY: the only other files allowed — the latest scene's map, one level deep
#   under map/, named <date>_<item_id> (no path separators in the id).
ALLOWED_MAP_PATTERN = re.compile(
    r"map/[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9_.-]+\.(?:tif|png)"
)


def is_allowed(name: str) -> bool:
    """True for a relative path `publish` may write (the C6 allowlist)."""
    return name in ALLOWED_FILES or ALLOWED_MAP_PATTERN.fullmatch(name) is not None


def resolve_out_dir(out_dir: Path) -> Path:
    """The real directory behind ``--out``, resolved once before any check.

    WHY: staging and the swap rename directories next to ``out_dir``; done
    on a symlink they would replace the link with a real directory and leave
    the old contract behind a stray ``.previous-*`` link. Resolving first
    makes the foreign check, staging and the swap all act on the target, so
    the link itself is never touched and keeps pointing at the new contract.
    A broken symlink (or a loop) is refused: resolving it would silently
    create a directory wherever the dangling link happens to point.
    """
    if out_dir.is_symlink() and not out_dir.exists():
        msg = f"--out {out_dir} is a broken symlink"
        raise PublishRefused(msg)
    try:
        return out_dir.resolve()
    except (OSError, RuntimeError) as exc:  # a loop in a parent component
        msg = f"--out {out_dir} cannot be resolved: {exc}"
        raise PublishRefused(msg) from exc


def check_out_dir(out_dir: Path) -> None:
    """Refuse an ``out_dir`` that publish could not safely replace.

    WHY: the swap renames ``out_dir`` away and deletes it, so it must only
    ever hold a previous contract. A missing or empty directory is fine; a
    non-empty one must contain ``publish.json`` and nothing outside the
    allowlist. Anything else (a file, a foreign directory) is refused before
    staging, so ``--out`` can never destroy unrelated data.

    WHY cheap first: ``--out $HOME`` must be refused at once, so
    ``publish.json`` is required at the top level before any walk, and the
    walk is lazy, stopping at the first entry outside the allowlist.
    """
    if not out_dir.exists() and not out_dir.is_symlink():
        return
    if not out_dir.is_dir():
        msg = f"--out {out_dir} exists and is not a directory"
        raise PublishRefused(msg)
    refusal = (
        f"--out {out_dir} is not a previous publish (no {INDEX_FILE} or "
        "files outside the allowlist)"
    )
    if not (out_dir / INDEX_FILE).is_file():
        if next(out_dir.iterdir(), None) is None:
            return
        raise PublishRefused(refusal)
    for path in out_dir.rglob("*"):
        rel = path.relative_to(out_dir).as_posix()
        if not is_allowed(rel) and rel != _MAP_DIR:
            raise PublishRefused(f"{refusal} (foreign: {rel})")


def _swap_in(staging: Path, out_dir: Path) -> None:
    """Replace ``out_dir`` with ``staging`` by rename; restore it on failure."""
    if not out_dir.exists():
        staging.rename(out_dir)
        return
    previous = out_dir.with_name(f".{out_dir.name}.previous-{staging.name}")
    out_dir.rename(previous)
    try:
        staging.rename(out_dir)
    except BaseException:
        previous.rename(out_dir)
        raise
    # WHY ignore errors: the new contract is already in place; a leftover of
    #   the previous one (e.g. a file someone holds open) must not turn a
    #   successful publish into a failure.
    shutil.rmtree(previous, ignore_errors=True)


def publish_index(
    staging: Path, run_id: str, series_sha256: str, generated_utc: str
) -> dict[str, object]:
    """Integrity index over every file staged in ``staging`` (not itself)."""
    paths = sorted(
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
    )
    return {
        "files": [
            {"path": rel, "sha256": series.sha256_file(staging / rel)} for rel in paths
        ],
        "generated_utc": generated_utc,
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "series_sha256": series_sha256,
        "smolf_version": __version__,
    }


def _write_contract(
    out_dir: Path,
    documents: dict[str, str],
    run_id: str,
    series_sha256: str,
    maps: list[tuple[str, str, PublishedMap]] | None = None,
) -> None:
    """Write allowlisted files into a sibling staging dir, then swap it in.

    Maps, given as (GeoTIFF path, PNG path, layer), go through the guarded
    raster writers only (uint8, classes 0-5). ``publish.json`` is built last
    from the bytes actually staged and validated before the swap.
    """
    maps = [] if maps is None else maps
    map_names = [name for tif, png, _ in maps for name in (tif, png)]
    for name in [*documents, *map_names]:
        if not is_allowed(name):
            msg = f"refusing to publish a file outside the allowlist: {name}"
            raise ValueError(msg)
    check_out_dir(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}.staging-", dir=out_dir.parent)
    )
    # WHY: mkdtemp creates a private (0700) dir; the contract is meant to be
    #   read by others, so it gets ordinary directory permissions.
    staging.chmod(0o755)
    try:
        for name, text in documents.items():
            (staging / name).write_text(text, encoding="utf-8")
        for tif, png, layer in maps:
            for name in (tif, png):
                (staging / name).parent.mkdir(mode=0o755, exist_ok=True)
            write_published_geotiff(
                staging / tif, layer.class_map, layer.transform, layer.crs
            )
            write_display_png(staging / png, layer.display)
        index = publish_index(staging, run_id, series_sha256, series.utc_now())
        validate("publish", index)
        (staging / INDEX_FILE).write_text(canonical_json(index), encoding="utf-8")
        _swap_in(staging, out_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def publish_run(
    run_id: str | None = None,
    out_dir: Path | None = None,
    series_root: Path | None = None,
    levels_path: Path | None = None,
) -> Path:
    """Publish the contract for one run; return ``out_dir`` as given.

    A symlinked ``out_dir`` is published through to its target; the link
    itself is never renamed.

    Every document is validated before anything is written. Raises
    PublishRefused (exit 2) or ContractInvalid (exit 1); on either, the
    previous output directory is left untouched.
    """
    # WHY late binding: defaults resolve at call time through `paths`.
    root = paths.series_dir() if series_root is None else Path(series_root)
    target = paths.publish_dir() if out_dir is None else Path(out_dir)
    real = resolve_out_dir(target)  # every step below acts on the real dir
    check_out_dir(real)  # fail fast; checked again right before staging
    run_dir = select_run(root, run_id)
    records = series.load_records(run_dir / "scenes")
    manifest = _read_manifest(run_dir)
    latest = latest_document(records, manifest, levels_path)
    series_doc = series_document(records, manifest)
    record = latest_record(records)
    layer = build_map(record, manifest)
    latest_map = _require_dict(latest["map"], "latest.json map")
    latest_map["bounds"] = layer.display.bounds
    validate("latest", latest)
    validate("series", series_doc)
    _write_contract(
        real,
        {
            "latest.json": canonical_json(latest),
            "series.json": canonical_json(series_doc),
        },
        str(manifest["run_id"]),
        str(manifest["series_sha256"]),
        [(map_path(record), png_path(record), layer)],
    )
    return target
