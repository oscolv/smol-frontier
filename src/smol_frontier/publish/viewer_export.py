"""Viewer export: one folder per scene for endho's per-date viewer (#102).

``export_viewer`` reads a verified publish, resolves its run like
``export maps``, and for every ``ok`` scene with figures (POOR excluded, no
endho gate) writes ``<date>_<item_id>/`` with:

- ``photo.webp``: the Sentinel-2 true-colour photo, built exactly like
  ``latest.image`` (map grid, RGBA clipped to the reservoir, fixed window
  and gamma), read from the item's COGs;
- ``classified.webp``: the published class map (0-5) on the same grid,
  lossless, decoded back and checked against the classes it was drawn from;
- ``figures.json``: the scene's ``series.json`` figures verbatim, hectares
  and percent of the reservoir per published class, and wet biomass
  (``biomass.wet_biomass`` on the corrected hectares).

plus ``index.json``, ``README.txt`` and ``SHA256SUMS``. Outside the
contract, like the other exports. Incremental: a scene folder of an earlier
export of the same run whose files still hash to its ``index.json`` entry,
and whose ``figures.json`` rebuilds byte for byte from the current
``series.json``, is copied instead of rebuilt; every other folder is built.
The output directory is always swapped in whole, so it never mixes runs.
"""

import hashlib
import json
import re
import tempfile
import warnings
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning

from smol_frontier import __version__, aoi, paths, series
from smol_frontier.biomass import wet_biomass
from smol_frontier.classify.display import (
    DisplayImage,
    decode_display_classes,
    write_display_webp,
)
from smol_frontier.config import settings
from smol_frontier.hydro.flood_map import PIXEL_HA
from smol_frontier.publish.documents import canonical_json
from smol_frontier.publish.errors import PublishRefused
from smol_frontier.publish.export import (
    _HOLDER,
    _LICENCE_URL,
    read_publish,
    resolve_export_dir,
    write_export,
)
from smol_frontier.publish.map import build_map, committed_curve
from smol_frontier.publish.map_export import _check_scene, _run_dir
from smol_frontier.publish.truecolour import (
    attribution,
    build_true_colour,
    to_rgba,
    write_true_colour_webp,
)

__all__ = ["COPIED_FIELDS", "SCENE_FILES", "export_viewer", "owns", "scene_figures"]

SCENE_FILES = ("photo.webp", "classified.webp", "figures.json")
_FIXED_FILES = frozenset({"index.json", "README.txt", "SHA256SUMS"})
_FOLDER = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9_.-]+")

# WHY: the series.json fields figures.json repeats verbatim (#102), so the
#   viewer's hectares, percent and level are the homepage's to the digit.
#   Corrected figures only: the public viewer never shows raw hyacinth.
COPIED_FIELDS = (
    "hyacinth_corrected_ha",
    "dense_corrected_ha",
    "interface_corrected_ha",
    "display_ha",
    "pct_reservoir_corrected",
    "cover_band",
    "display_status",
    "removed_ha",
    "level_m",
    "valid_frac",
    "cloud_frac",
    "bed_applied",
    "quality_light",
    "regime",
)

# WHY: (class code, figures.json key) for every class the published legend
#   shows; no data is the reservoir area the classes leave.
_CLASSES = (
    (settings.CLASS_WATER, "water"),
    (settings.CLASS_DENSE_HYACINTH, "dense_hyacinth"),
    (settings.CLASS_INTERFACE, "interface"),
    (settings.CLASS_BARE_SOIL, "bare_soil"),
    (settings.CLASS_BED_REMOVED, "bed_removed"),
)
# WHY: classes are counted in whole pixels inside the polygon (by centre),
#   which can exceed the polygon's own area by a rounding margin; no data
#   is then clamped at 0. Beyond this margin the classes disagree with it.
# HOW: measured: the polygon rasterised on the scene grid is 1261.76 ha
#   against the 1261.75 ha polygon (0.01 ha); 0.05 ha leaves room for the
#   per-class rounding to the hundredth.
_AREA_SLACK_HA = 0.05
# WHY: a photo read failure from one of ~470 COG reads should not throw away
#   a half-hour export; a scene that fails this many times refuses it.
_PHOTO_ATTEMPTS = 3
# WHY: concurrent photo reads; enough to hide the network latency without
#   hammering the STAC/COG hosts.
# HOW: 6 keeps the photo reads (~10 s each) ahead of the ~1.3 s map
#   rebuild of the next scenes.
_PHOTO_WORKERS = 6


def owns(name: str) -> bool:
    """True for the names the viewer export writes (its out-dir allowlist)."""
    if name in _FIXED_FILES or _FOLDER.fullmatch(name):
        return True
    folder, _, file = name.partition("/")
    return _FOLDER.fullmatch(folder) is not None and file in SCENE_FILES


def _reservoir_ha() -> float:
    return round(aoi.reservoir_area_ha(), 2)


def class_hectares(class_map: np.ndarray) -> dict[int, float]:
    """Hectares of each published class 1-5 on the scene's native grid."""
    counts = np.bincount(np.asarray(class_map).ravel(), minlength=6)
    return {code: round(float(counts[code]) * PIXEL_HA, 2) for code, _ in _CLASSES}


def scene_figures(
    scene: dict[str, object],
    hectares: dict[int, float],
    bounds: list[list[float]],
    run_id: str,
) -> dict[str, object]:
    """The figures.json document of one scene (pure).

    Percentages and biomass are null on LOWER_BOUND scenes, where only
    hectares are meaningful; biomass is null when there is no corrected
    hyacinth area.
    """
    reservoir = _reservoir_ha()
    lower_bound = scene["display_status"] == "LOWER_BOUND"
    named = {key: hectares[code] for code, key in _CLASSES}
    no_data = round(reservoir - sum(named.values()), 2)
    if no_data < -_AREA_SLACK_HA:
        msg = (
            f"classes of {scene['item_id']} cover {sum(named.values()):.2f} ha, "
            f"more than the {reservoir} ha reservoir"
        )
        raise PublishRefused(msg)
    named["no_data"] = max(no_data, 0.0)
    classes = {
        key: {
            "ha": value,
            "pct_reservoir": None if lower_bound else round(value * 100 / reservoir, 2),
        }
        for key, value in named.items()
    }
    mass = None if lower_bound else wet_biomass(scene["hyacinth_corrected_ha"])
    return {
        "date": scene["date"],
        "item_id": scene["item_id"],
        "run_id": run_id,
        "bounds": bounds,
        "attribution": attribution(scene["date"]),
        **{field: scene[field] for field in COPIED_FIELDS},
        "reservoir_ha": reservoir,
        "classes": classes,
        "biomass_wet_t": None if mass is None else mass.t,
        "biomass_wet_t_low": None if mass is None else mass.t_low,
        "biomass_wet_t_high": None if mass is None else mass.t_high,
        "biomass_density_kg_m2": {
            "central": settings.BIOMASS_WET_KG_M2,
            "low": settings.BIOMASS_WET_KG_M2_LOW,
            "high": settings.BIOMASS_WET_KG_M2_HIGH,
        },
        "biomass_source": settings.BIOMASS_SOURCE,
    }


def _json_bytes(document: dict[str, object]) -> bytes:
    return canonical_json(document).encode("utf-8")


def _decoded_rgba(data: bytes) -> np.ndarray:
    # WHY the filter: the WebP carries no georeference by design.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.MemoryFile(data) as mem, mem.open() as src:
            return src.read()


def _photo(record: dict[str, object], display: DisplayImage) -> bytes:
    """The scene's photo.webp, read from its COGs (retried on failure)."""
    error: PublishRefused | None = None
    for _ in range(_PHOTO_ATTEMPTS):
        try:
            photo = build_true_colour(record, display)
            break
        except PublishRefused as exc:
            error = exc
    else:
        assert error is not None
        raise error
    rgba = to_rgba(photo.reflectance, display.transform)
    with tempfile.TemporaryDirectory() as tmp:
        path = write_true_colour_webp(Path(tmp) / "photo.webp", rgba, photo.collection)
        return path.read_bytes()


def _build_scene(
    record: dict[str, object],
    scene: dict[str, object],
    manifest: dict[str, object],
    committed: object,
    run_id: str,
    tmp: Path,
) -> tuple[dict[str, bytes], DisplayImage]:
    """Rebuild, check and render one scene's map and figures.

    Returns ``classified.webp`` and ``figures.json`` and the display image
    the photo is read onto.
    """
    layer = build_map(record, manifest, committed)
    _check_scene(layer.class_map, scene)
    classified = write_display_webp(tmp / "classified.webp", layer.display)
    classified_bytes = classified.read_bytes()
    decoded = decode_display_classes(_decoded_rgba(classified_bytes))
    if not np.array_equal(decoded, layer.display.classes):
        msg = f"classified.webp of {scene['item_id']} does not decode to its classes"
        raise PublishRefused(msg)
    figures = scene_figures(
        scene, class_hectares(layer.class_map), layer.display.bounds, run_id
    )
    files = {"classified.webp": classified_bytes, "figures.json": _json_bytes(figures)}
    return files, layer.display


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _report(progress: Callable[[str], None] | None, line: str) -> None:
    if progress is not None:
        progress(line)


def _reusable(real: Path, run_id: str) -> dict[str, dict[str, bytes]]:
    """Scene folders of an earlier export of ``run_id`` whose files still
    hash to its index.json (else nothing)."""
    try:
        index = json.loads((real / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(index, dict) or index.get("run_id") != run_id:
        return {}
    found: dict[str, dict[str, bytes]] = {}
    for entry in index.get("scenes", []):
        folder = entry.get("folder")
        if not isinstance(folder, str) or not _FOLDER.fullmatch(folder):
            continue
        try:
            files = {name: (real / folder / name).read_bytes() for name in SCENE_FILES}
        except OSError:
            continue
        if all(_sha(files[name]) == entry["files"].get(name) for name in SCENE_FILES):
            found[folder] = files
    return found


def _still_current(
    files: dict[str, bytes], scene: dict[str, object], run_id: str
) -> bool:
    """True when the old figures.json rebuilds byte for byte from ``scene``."""
    try:
        old = json.loads(files["figures.json"])
        hectares = {code: float(old["classes"][key]["ha"]) for code, key in _CLASSES}
        rebuilt = scene_figures(scene, hectares, old["bounds"], run_id)
    except (KeyError, TypeError, ValueError, PublishRefused):
        return False
    return _json_bytes(rebuilt) == files["figures.json"]


def _readme(run_id: str, series_json_sha256: str, count: int, year: str) -> bytes:
    lines = [
        "Water hyacinth on Endho Reservoir: per-date viewer archive",
        "==========================================================",
        "",
        f"One folder per scene with figures in run {run_id} ({count} scenes),",
        "named <date>_<item_id>/. DOUBTFUL scenes are included; POOR scenes",
        "have no figures and no folder. Each folder holds:",
        "",
        "  photo.webp       Sentinel-2 true colour, EPSG:3857, clipped to the",
        "                   reservoir (RGBA), reflectance 0-0.30, gamma 1.6",
        "  classified.webp  the published class map (classes 0-5, exposed-bed",
        "                   correction applied), lossless, same grid and bounds",
        "  figures.json     the scene's series.json figures, hectares and",
        "                   percent per class, and wet biomass",
        "",
        "bounds are [[south, west], [north, east]] in EPSG:4326 and shared by",
        "both images. Wet biomass = corrected hyacinth hectares x the field",
        (
            f"density {settings.BIOMASS_WET_KG_M2} kg/m2 (range "
            f"{settings.BIOMASS_WET_KG_M2_LOW}-{settings.BIOMASS_WET_KG_M2_HIGH}),"
        ),
        "an order-of-magnitude estimate. Percentages and biomass are null on",
        "lower-bound scenes (less than 95 % of the reservoir visible).",
        "",
        "index.json ties every folder to the run (run_id, series_sha256) and",
        "to the series.json it matches:",
        f"    {series_json_sha256}",
        "To check the files, run `sha256sum -c SHA256SUMS` in this folder.",
        "",
        f"Licence: (C) {year} {_HOLDER}. Released under",
        "the Creative Commons Attribution 4.0 International license",
        f"(CC BY 4.0): {_LICENCE_URL}",
        "Contains modified Copernicus Sentinel data (Sentinel-2 L2A).",
        "These figures are remote-sensing estimates, not an official measurement.",
        "",
    ]
    return "\n".join(lines).encode("ascii")


def _sha256sums(files: dict[str, bytes]) -> bytes:
    return "".join(
        f"{_sha(data)}  {name}\n" for name, data in sorted(files.items())
    ).encode("ascii")


def export_viewer(
    from_dir: Path | None = None,
    out_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Export the per-date viewer archive of a verified publish.

    Defaults resolve at call time: ``paths.publish_dir()`` and
    ``paths.viewer_dir()``. Every scene is built and checked before anything
    is written; a refusal (PublishRefused, exit 2) leaves ``out_dir`` as it
    was. ``progress`` receives one line per scene.
    """
    source = paths.publish_dir() if from_dir is None else Path(from_dir)
    target = paths.viewer_dir() if out_dir is None else Path(out_dir)
    series_doc, latest_doc, series_json_sha256 = read_publish(source)
    real = resolve_export_dir(source, target)
    run_dir, manifest = _run_dir(series_doc)
    run_id = str(series_doc["run_id"])
    records = [
        record
        for record in sorted(
            series.load_records(run_dir / "scenes"), key=series.record_sort_key
        )
        if record["status"] == "ok"
    ]
    scenes = {(s["date"], s["item_id"]): s for s in series_doc["scenes"]}
    reusable = _reusable(real, run_id)
    committed = committed_curve(manifest)
    built: dict[str, dict[str, bytes]] = {}
    photos: dict[str, Future[bytes]] = {}
    # WHY threads for the photos only: a photo is a network read of three
    #   COG windows (I/O-bound, GDAL releases the GIL), ~10 s each, while
    #   the map rebuild is ~1 s of CPU; reading them alongside cuts the
    #   ~470-scene export from hours to the map rebuild's own time.
    with (
        tempfile.TemporaryDirectory() as tmp,
        ThreadPoolExecutor(max_workers=_PHOTO_WORKERS) as pool,
    ):
        try:
            for record in records:
                key = (record["date"], record["item_id_used"])
                if key not in scenes:
                    msg = f"scene {key[1]} of run {run_id} is not in series.json"
                    raise PublishRefused(msg)
                scene = scenes[key]
                # WHY: a scene without figures (POOR) has nothing to show.
                if scene["hyacinth_raw_ha"] is None:
                    continue
                folder = f"{record['date']}_{record['item_id_used']}"
                old = reusable.get(folder)
                if old is not None and _still_current(old, scene, run_id):
                    built[folder] = old
                    _report(progress, f"kept {folder}")
                    continue
                files, display = _build_scene(
                    record, scene, manifest, committed, run_id, Path(tmp)
                )
                built[folder] = files
                photos[folder] = pool.submit(_photo, record, display)
                photos[folder].add_done_callback(
                    lambda _, folder=folder: _report(progress, f"built {folder}")
                )
            for folder, future in photos.items():
                built[folder]["photo.webp"] = future.result()
        except BaseException:
            # WHY: a refusal must not wait for every queued photo download.
            pool.shutdown(cancel_futures=True)
            raise
    files: dict[str, bytes] = {}
    entries = []
    for folder, scene_files in built.items():
        date, item_id = folder.split("_", 1)
        for name in SCENE_FILES:
            files[f"{folder}/{name}"] = scene_files[name]
        entries.append(
            {
                "date": date,
                "item_id": item_id,
                "folder": folder,
                "files": {name: _sha(scene_files[name]) for name in SCENE_FILES},
            }
        )
    index = {
        "run_id": run_id,
        "series_sha256": series_doc["series_sha256"],
        "series_json_sha256": series_json_sha256,
        "smolf_version": __version__,
        "scenes": entries,
    }
    files["index.json"] = _json_bytes(index)
    files["README.txt"] = _readme(
        run_id, series_json_sha256, len(entries), str(latest_doc["date"])[:4]
    )
    files["SHA256SUMS"] = _sha256sums(files)
    write_export(real, files, target, owns)
    return target
