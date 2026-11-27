"""Map export: one classified GeoTIFF per scene of a finished publish.

``export_maps`` reads a verified publish, resolves the series run it came
from, builds every ``ok`` scene's published class map (classes 0-5)
through ``map.build_map`` (cache hash, per-class and haze hectares, input
hashes and removed hectares checked against the run), checks each map's
pixel counts against that scene's ``series.json`` figures (scenes without
figures, i.e. POOR, get no map), and swaps
``<date>_<item_id>.tif`` files, ``index.csv``, ``README.txt`` and
``SHA256SUMS`` into the output directory. Like the CSV bundle it is outside
the contract: nothing here changes the published files or their schemas.
It reads the scene cache (re-ingesting a missing scene by its exact id).

The export is incremental across runs: a map of the previous export whose
``index.csv`` row has the same file name and ``map_key``, was written by
the same smolf version and config, and still hashes to its row is kept
byte for byte (and re-checked against its new ``series.json`` scene;
rebuilt if it disagrees). Only new or changed scenes are built. The rows,
``README.txt`` and ``SHA256SUMS`` always describe the new run, and the
maps equal those of a full export of that run into an empty directory.
"""

import csv
import hashlib
import io
import re
import tempfile
from pathlib import Path

import numpy as np
from rasterio.errors import RasterioError
from rasterio.io import MemoryFile

from smol_frontier import __version__, paths, series
from smol_frontier.classify.geotiff import (
    PUBLISHED_CLASS_NAMES,
    PUBLISHED_COLORMAP,
    write_published_geotiff,
)
from smol_frontier.config import settings
from smol_frontier.hydro.flood_map import PIXEL_HA
from smol_frontier.publish.errors import PublishRefused
from smol_frontier.publish.export import (
    _HOLDER,
    _LICENCE_URL,
    _csv_bytes,
    read_publish,
    resolve_export_dir,
    write_export,
)
from smol_frontier.publish.map import build_map, committed_curve, map_path
from smol_frontier.publish.map_key import map_key
from smol_frontier.publish.select import _read_manifest

__all__ = ["INDEX_COLUMNS", "export_maps", "owns"]

INDEX_COLUMNS = (
    "date",
    "item_id",
    "file",
    "sha256",
    "bytes",
    "run_id",
    "series_sha256",
    "series_json_sha256",
    # WHY: the reuse keys, after the eight original columns so readers of
    #   those keep working; version and config repeat per row so the index
    #   stays one self-contained file.
    "map_key",
    "smolf_version",
    "config_hash",
)
_FIXED_FILES = frozenset({"index.csv", "README.txt", "SHA256SUMS"})
# WHY: the published map's file name without its map/ directory.
_MAP_FILE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9_.-]+\.tif")
# WHY: (class code, series.json field when the bed correction applied, when
#   it did not). Every reservoir pixel is staged in the flood map, so the
#   corrected figures are exactly the map's class-2 and class-3 pixels.
_HYACINTH_FIELDS = (
    (settings.CLASS_DENSE_HYACINTH, "dense_corrected_ha", "dense_ha"),
    (settings.CLASS_INTERFACE, "interface_corrected_ha", "interface_ha"),
)


def owns(name: str) -> bool:
    """True for the names the map export writes (its out-dir allowlist)."""
    return name in _FIXED_FILES or _MAP_FILE.fullmatch(name) is not None


def _file_name(record: dict[str, object]) -> str:
    """``<date>_<item_id>.tif``: the published map's name, flat."""
    return map_path(record).split("/", 1)[1]


def _run_dir(series_doc: dict[str, object]) -> tuple[Path, dict[str, object]]:
    """The run behind ``series_doc`` and its manifest, else refused."""
    run_id = str(series_doc["run_id"])
    run_dir = paths.series_dir() / run_id
    if not (run_dir / "manifest.json").is_file():
        msg = f"run {run_id} of the publish is not under {paths.series_dir()}"
        raise PublishRefused(msg)
    manifest = _read_manifest(run_dir)
    if manifest.get("status") != "complete":
        msg = f"run {run_id} is not complete (status {manifest.get('status')})"
        raise PublishRefused(msg)
    actual = series.sha256_file(run_dir / "series.csv")
    for name, value in (
        ("manifest series_sha256", manifest.get("series_sha256")),
        ("series.csv sha256", actual),
    ):
        if value != series_doc["series_sha256"]:
            msg = (
                f"run {run_id} {name} ({value}) differs from the publish "
                f"series_sha256 ({series_doc['series_sha256']})"
            )
            raise PublishRefused(msg)
    return run_dir, manifest


def _check_scene(class_map: np.ndarray, scene: dict[str, object]) -> None:
    """Refuse a map whose pixel counts disagree with its series.json scene."""
    counts = np.bincount(np.asarray(class_map).ravel(), minlength=6)
    applied = scene["bed_applied"] is True
    expected = [
        (code, scene[corrected if applied else raw])
        for code, corrected, raw in _HYACINTH_FIELDS
    ]
    expected.append((settings.CLASS_BED_REMOVED, scene["removed_ha"] or 0.0))
    mismatches = [
        f"class {code} {round(counts[code] * PIXEL_HA, 2)} ha != {value}"
        for code, value in expected
        if round(counts[code] * PIXEL_HA, 2) != round(float(value), 2)
    ]
    if mismatches:
        msg = f"map of {scene['item_id']} differs from series.json: " + "; ".join(
            mismatches
        )
        raise PublishRefused(msg)


def _reusable(real: Path, config_hash: str) -> dict[str, tuple[str, bytes]]:
    """Maps of the previous export that still hash to its index.csv, by
    file name, with their ``map_key`` (else nothing).

    WHY version and config must match: the map build is smolf's code and
    settings, so another version or config rebuilds every map.
    """
    try:
        text = (real / "index.csv").read_text(encoding="utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
    except (OSError, ValueError, csv.Error):
        return {}
    found: dict[str, tuple[str, bytes]] = {}
    for row in rows:
        if (
            row.get("smolf_version") != __version__
            or row.get("config_hash") != config_hash
            or not row.get("map_key")
        ):
            continue
        name = row.get("file")
        if not isinstance(name, str) or _MAP_FILE.fullmatch(name) is None:
            continue
        try:
            data = (real / name).read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() == row.get("sha256"):
            found[name] = (row["map_key"], data)
    return found


def _kept_agrees(data: bytes, scene: dict[str, object]) -> bool:
    """True when a kept map's pixel counts still match its series.json scene."""
    try:
        with MemoryFile(data) as memory, memory.open() as src:
            if src.count != 1 or src.dtypes[0] != "uint8":
                return False
            class_map = src.read(1)
    except RasterioError:
        return False
    try:
        _check_scene(class_map, scene)
    except PublishRefused:
        return False
    return True


def _readme(run_id: str, series_json_sha256: str, count: int, year: str) -> bytes:
    """The map export's README: files, classes, licence."""
    classes = [
        f"  {code}  {PUBLISHED_CLASS_NAMES[code]:<36}"
        f"#{''.join(f'{c:02x}' for c in PUBLISHED_COLORMAP[code][:3])}"
        for code in sorted(PUBLISHED_CLASS_NAMES)
    ]
    lines = [
        "Water hyacinth on Endho Reservoir: per-scene class maps",
        "=======================================================",
        "",
        f"One classified GeoTIFF per scene with figures in run {run_id}",
        f"({count} maps), named <date>_<item_id>.tif. DOUBTFUL scenes are",
        "included; POOR scenes have no figures and get no map. Each map is",
        "single-band uint8, nodata 0, EPSG:32614, 10 m, tiled 256x256,",
        "DEFLATE, with an embedded colour table and band tags",
        "class_0..class_5. Each pixel is 0.01 ha.",
        "",
        "Classes:",
        *classes,
        "",
        "Class 5 is the hyacinth the exposed-bed correction removed, so its",
        "pixels x 0.01 ha equal the scene's removed_ha in series.json.",
        "",
        "index.csv names, for every map, its scene, sha256 and size, the run",
        "(run_id, series_sha256) and the sha256 of the series.json it",
        "matches:",
        f"    {series_json_sha256}",
        "To check the files, run `sha256sum -c SHA256SUMS` in this folder.",
        "",
        f"Licence: (C) {year} {_HOLDER}. Released under",
        "the Creative Commons Attribution 4.0 International license",
        f"(CC BY 4.0): {_LICENCE_URL}",
        "Contains modified Copernicus Sentinel data (Sentinel-2 L2A).",
        "These maps are remote-sensing estimates, not an official measurement.",
        "",
    ]
    return "\n".join(lines).encode("ascii")


def _sha256sums(files: dict[str, bytes]) -> bytes:
    """``sha256sum -c`` lines for every file but itself, sorted by name."""
    return "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(files.items())
    ).encode("ascii")


def export_maps(from_dir: Path | None = None, out_dir: Path | None = None) -> Path:
    """Export every scene map of a verified publish; return ``out_dir`` as given.

    Defaults resolve at call time: ``paths.publish_dir()`` and
    ``paths.maps_dir()``. Every map is kept (see the module docstring) or
    built, and checked, before anything is written; a refusal
    (PublishRefused, exit 2) leaves ``out_dir`` as it was.
    """
    source = paths.publish_dir() if from_dir is None else Path(from_dir)
    target = paths.maps_dir() if out_dir is None else Path(out_dir)
    series_doc, latest_doc, series_json_sha256 = read_publish(source)
    real = resolve_export_dir(source, target)
    run_dir, manifest = _run_dir(series_doc)
    records = [
        record
        for record in sorted(
            series.load_records(run_dir / "scenes"), key=series.record_sort_key
        )
        if record["status"] == "ok"
    ]
    scenes = {(s["date"], s["item_id"]): s for s in series_doc["scenes"]}
    committed = committed_curve(manifest)
    config_hash = series.config_hash()
    reusable = _reusable(real, config_hash)
    files: dict[str, bytes] = {}
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for record in records:
            key = (record["date"], record["item_id_used"])
            if key not in scenes:
                msg = (
                    f"scene {key[1]} of run {series_doc['run_id']} is not in "
                    "series.json"
                )
                raise PublishRefused(msg)
            # WHY: a scene without figures (POOR) has nothing a map could be
            #   checked against, and endho-web never shows it.
            if scenes[key]["hyacinth_raw_ha"] is None:
                continue
            name = _file_name(record)
            scene_map_key = map_key(record, manifest)
            old_key, data = reusable.get(name, (None, b""))
            if old_key != scene_map_key or not _kept_agrees(data, scenes[key]):
                layer = build_map(record, manifest, committed)
                _check_scene(layer.class_map, scenes[key])
                path = write_published_geotiff(
                    Path(tmp) / name, layer.class_map, layer.transform, layer.crs
                )
                data = path.read_bytes()
            files[name] = data
            rows.append(
                [
                    str(record["date"]),
                    str(record["item_id_used"]),
                    name,
                    hashlib.sha256(data).hexdigest(),
                    str(len(data)),
                    str(series_doc["run_id"]),
                    str(series_doc["series_sha256"]),
                    series_json_sha256,
                    scene_map_key,
                    __version__,
                    config_hash,
                ]
            )
    files["index.csv"] = _csv_bytes(INDEX_COLUMNS, rows)
    files["README.txt"] = _readme(
        str(series_doc["run_id"]),
        series_json_sha256,
        len(rows),
        str(latest_doc["date"])[:4],
    )
    files["SHA256SUMS"] = _sha256sums(files)
    write_export(real, files, target, owns)
    return target
