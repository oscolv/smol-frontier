"""Writer: allowlist, ``--out`` check, staging, atomic swap, the
``publish.json`` index and the ``publish_run`` orchestration."""

import os
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np

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
from smol_frontier.publish.truecolour import (
    IMAGE_DIR,
    build_true_colour,
    image_path,
    stretch,
    write_true_colour_webp,
)

# WHY: allowlist — the only files `publish` may ever write, so nothing
#   unexpected (e.g. PlanetScope imagery) can reach the published directory.
ALLOWED_FILES = frozenset({"latest.json", "series.json", "publish.json"})
# WHY: the integrity index is written last, over the other staged files; it
#   is the only file that never lists itself.
INDEX_FILE = "publish.json"

# WHY: the only other files allowed — the latest scene's map, one level deep
#   under map/, named <date>_<item_id> (no path separators in the id).
ALLOWED_MAP_PATTERN = re.compile(
    r"map/[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9_.-]+\.(?:tif|png)"
)

# WHY: Sentinel-2 photos (contract 3.0.0) — the latest scene's under
#   image/, the reservoir-clipped same-month ones under image/clipped/, both
#   named <date>_<item_id>.webp so a new document never pairs with an old
#   image.
ALLOWED_IMAGE_PATTERN = re.compile(
    r"image/(?:clipped/)?[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9_.-]+\.webp"
)

# WHY: the only directories a contract holds (the walk sees them as entries).
_ALLOWED_DIRS = frozenset({_MAP_DIR, IMAGE_DIR, f"{IMAGE_DIR}/clipped"})


def is_allowed(name: str) -> bool:
    """True for a relative path `publish` may write (the C6 allowlist)."""
    return (
        name in ALLOWED_FILES
        or ALLOWED_MAP_PATTERN.fullmatch(name) is not None
        or ALLOWED_IMAGE_PATTERN.fullmatch(name) is not None
    )


def _resolve_out_dir(out_dir: Path) -> Path:
    """The real directory behind ``--out``, resolved once before any check.

    WHY: staging and the swap rename directories next to ``out_dir``; done
    on a symlink they would replace the link with a real directory and leave
    the old contract behind a stray ``.previous-*`` link. Resolving first
    makes the foreign check, staging and the swap all act on the target, so
    the link itself is never touched and keeps pointing at the new contract.
    A broken symlink (or a loop) anywhere in the path, the last component
    or an ancestor, is refused: resolving it would silently create a
    directory wherever the dangling link points (e.g. ``outputs/`` linked to
    an unmounted drive would publish onto the underlying filesystem). A
    missing parent with no symlink in the way is still created later.

    WHY one exception: a crash mid-swap through a symlinked ``--out``
    leaves the link dangling (its target was renamed to ``.previous-*``).
    When only the final component dangles and its target's parent exists,
    the target gets the same restore as a plain ``--out``; if that restores
    nothing, the link stays refused. A dangling ancestor is always refused.
    """
    for part in out_dir.absolute().parents:
        if part.is_symlink() and not part.exists():
            msg = f"--out {out_dir} goes through a broken symlink: {part}"
            raise PublishRefused(msg)
    if out_dir.is_symlink() and not out_dir.exists():
        target = _link_target(out_dir)
        if target is None or not _restore_previous(target):
            msg = f"--out {out_dir} goes through a broken symlink: {out_dir}"
            raise PublishRefused(msg)
    try:
        return out_dir.resolve()
    except (OSError, RuntimeError) as exc:  # a loop in a parent component
        msg = f"--out {out_dir} cannot be resolved: {exc}"
        raise PublishRefused(msg) from exc


def _link_target(link: Path) -> Path | None:
    """The real path a dangling ``link`` names, if its parent exists.

    WHY one level only: a target that is itself a link, or whose parent is
    missing (an unmounted drive), is not a crash publish could have caused.
    """
    target = Path(link.absolute().parent, os.readlink(link))
    if target.name in ("", ".", ".."):
        return None
    try:
        parent = target.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not parent.is_dir():
        return None
    return parent / target.name


def check_out_dir(out_dir: Path, given: Path | None = None) -> None:
    """Refuse an ``out_dir`` that publish could not safely replace.

    ``given`` is the ``--out`` as typed, named in refusals when it differs
    from the resolved ``out_dir`` (a symlinked ``--out``).

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
    linked = given is not None and given.absolute() != out_dir
    shown = f"{given} (-> {out_dir})" if linked else out_dir
    if not out_dir.is_dir():
        msg = f"--out {shown} exists and is not a directory"
        raise PublishRefused(msg)
    refusal = (
        f"--out {shown} is not a previous publish (no {INDEX_FILE} or "
        "files outside the allowlist)"
    )
    if not (out_dir / INDEX_FILE).is_file():
        if next(out_dir.iterdir(), None) is None:
            return
        raise PublishRefused(refusal)
    foreign = _first_foreign(out_dir)
    if foreign is not None:
        raise PublishRefused(f"{refusal} (foreign: {foreign})")


def _first_foreign(directory: Path) -> str | None:
    """The first entry under ``directory`` outside the allowlist, if any."""
    for path in directory.rglob("*"):
        rel = path.relative_to(directory).as_posix()
        if not is_allowed(rel) and rel not in _ALLOWED_DIRS:
            return rel
    return None


def _debris(out_dir: Path, kind: str) -> list[Path]:
    """Real directories next to ``out_dir`` named like publish's own debris.

    WHY only these: `_debris_pattern` gives the exact names
    `_write_contract` (staging) and `_swap_in` (previous) create, so
    nothing else is ever touched.
    WHY no symlinks: a link with such a name was not made by publish, and
    following it could reach (and delete) data anywhere.
    """
    if not out_dir.parent.is_dir():
        return []
    pattern = _debris_pattern(out_dir.name, kind)
    return sorted(
        path
        for path in out_dir.parent.iterdir()
        if pattern.fullmatch(path.name) and not path.is_symlink() and path.is_dir()
    )


def _recover_out_dir(out_dir: Path) -> None:
    """Undo what a crash mid-publish left next to ``out_dir`` (before checks).

    WHY: a process killed between the swap's two renames leaves ``out_dir``
    missing, the last good contract in ``.previous-*`` and a ``.staging-*``.
    A lone ``.previous-*`` is renamed back so the contract is never lost
    (the caller then checks it like any ``--out``); with several there is no
    safe choice, so publish refuses and touches nothing. Stale staging dirs
    are removed only if everything in them is allowlisted.
    """
    _restore_previous(out_dir)
    for staging in _debris(out_dir, "staging"):
        if _first_foreign(staging) is None:
            shutil.rmtree(staging, ignore_errors=True)


def _restore_previous(out_dir: Path) -> bool:
    """Rename a lone ``.previous-*`` back to a missing ``out_dir``.

    Returns True when it restored one. With several it refuses and touches
    nothing: there is no safe way to tell which one was the last good one.
    """
    if out_dir.exists() or out_dir.is_symlink():
        return False
    previous = _debris(out_dir, "previous")
    if not previous:
        return False
    if len(previous) > 1:
        names = ", ".join(path.name for path in previous)
        msg = (
            f"--out {out_dir} is missing and several previous publishes "
            f"remain ({names}); restore one by hand"
        )
        raise PublishRefused(msg)
    previous[0].rename(out_dir)
    return True


def _drop_stale_previous(out_dir: Path) -> None:
    """Remove ``.previous-*`` leftovers once ``out_dir`` holds a contract.

    WHY after check_out_dir: until ``out_dir`` is a valid contract, a
    leftover may be the only good copy. Each one must itself pass the
    ``--out`` rule with ``publish.json`` present; anything else is kept.
    """
    if not (out_dir / INDEX_FILE).is_file():
        return
    for previous in _debris(out_dir, "previous"):
        if (previous / INDEX_FILE).is_file() and _first_foreign(previous) is None:
            shutil.rmtree(previous, ignore_errors=True)


def _staging_prefix(name: str) -> str:
    """Prefix of the staging dir ``mkdtemp`` creates next to ``--out``."""
    return f".{name}.staging-"


def _previous_name(name: str, staging_name: str) -> str:
    """Name the swap gives the old ``--out`` while staging moves in."""
    return f".{name}.previous-{staging_name}"


def _debris_pattern(name: str, kind: str) -> re.Pattern[str]:
    """The exact names publish gives its own debris next to ``--out``.

    WHY exact: a prefix match on ``.a.staging-`` would also catch
    ``.a.staging-b.previous-…``, the previous contract of an ``--out``
    named ``a.staging-b``. The ``mkdtemp`` suffix never holds a dot or a
    separator, so ``[^./]+`` ends the staging name unambiguously.
    """
    staging = re.escape(_staging_prefix(name)) + r"[^./]+"
    if kind == "staging":
        return re.compile(staging)
    return re.compile(re.escape(_previous_name(name, "")) + staging)


def _swap_in(staging: Path, out_dir: Path) -> None:
    """Replace ``out_dir`` with ``staging`` by rename; restore it on failure."""
    if not out_dir.exists():
        staging.rename(out_dir)
        return
    previous = out_dir.with_name(_previous_name(out_dir.name, staging.name))
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
    images: list[tuple[str, np.ndarray, str | None]] | None = None,
) -> None:
    """Write allowlisted files into a sibling staging dir, then swap it in.

    Maps, given as (GeoTIFF path, PNG path, layer), go through the guarded
    raster writers only (uint8, classes 0-5). ``publish.json`` is built last
    from the bytes actually staged and validated before the swap.
    """
    maps = [] if maps is None else maps
    images = [] if images is None else images
    map_names = [name for tif, png, _ in maps for name in (tif, png)]
    image_names = [name for name, _, _ in images]
    for name in [*documents, *map_names, *image_names]:
        if not is_allowed(name):
            msg = f"refusing to publish a file outside the allowlist: {name}"
            raise ValueError(msg)
    check_out_dir(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=_staging_prefix(out_dir.name), dir=out_dir.parent)
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
        for name, array, collection in images:
            (staging / name).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            write_true_colour_webp(staging / name, array, collection)
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
    previous contract is never lost. Only publish's own leftovers (stale
    staging, restorable or superseded previous dirs) may be cleaned up
    first.
    """
    # WHY late binding: defaults resolve at call time through `paths`.
    root = paths.series_dir() if series_root is None else Path(series_root)
    target = paths.publish_dir() if out_dir is None else Path(out_dir)
    real = _resolve_out_dir(target)  # every step below acts on the real dir
    _recover_out_dir(real)  # a crash mid-swap may have left debris
    check_out_dir(real, target)  # fail fast; checked again right before staging
    _drop_stale_previous(real)
    run_dir = select_run(root, run_id)
    records = series.load_records(run_dir / "scenes")
    manifest = _read_manifest(run_dir)
    latest = latest_document(records, manifest, levels_path)
    series_doc = series_document(records, manifest)
    record = latest_record(records)
    layer = build_map(record, manifest)
    latest_map = _require_dict(latest["map"], "latest.json map")
    latest_map["bounds"] = layer.display.bounds
    photo = build_true_colour(record, layer.display)
    latest_image = _require_dict(latest["image"], "latest.json image")
    latest_image["bounds"] = layer.display.bounds
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
        [(image_path(record), stretch(photo.reflectance), photo.collection)],
    )
    return target
