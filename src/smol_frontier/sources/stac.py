"""Scene search over STAC.

One interface, two interchangeable backends (Microsoft Planetary Computer and
Element84 Earth Search). On timeout or HTTP error the search automatically
falls back to the other backend — the free Planetary Computer tier is often
degraded — and logs which backend served the result.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime

import planetary_computer as pc
import pystac
from pystac_client import Client
from pystac_client.exceptions import APIError
from requests.exceptions import RequestException

from smol_frontier.aoi import wgs84_bbox

logger = logging.getLogger(__name__)

__all__ = [
    "BAND_ALIASES",
    "EARTH_SEARCH",
    "PLANETARY_COMPUTER",
    "REQUIRED_BANDS",
    "SceneMeta",
    "deduplicate",
    "get_scene",
    "item_to_scene",
    "search_scenes",
]

PLANETARY_COMPUTER = "planetary-computer"
EARTH_SEARCH = "earth-search"

_BACKENDS = {
    PLANETARY_COMPUTER: {
        "url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "collection": "sentinel-2-l2a",
        "signed": True,
    },
    EARTH_SEARCH: {
        "url": "https://earth-search.aws.element84.com/v1",
        "collection": "sentinel-2-l2a",
        "signed": False,
    },
}

# WHY: the two backends name the same Sentinel-2 bands differently
#   (PC uses "B02", Earth Search uses "blue"), so every lookup tries the
#   aliases in order and scenes always expose canonical band names.
BAND_ALIASES: dict[str, tuple[str, ...]] = {
    "B02": ("B02", "blue"),
    "B03": ("B03", "green"),
    "B04": ("B04", "red"),
    "B05": ("B05", "rededge1"),
    "B08": ("B08", "nir"),
    "B8A": ("B8A", "nir08"),
    "B11": ("B11", "swir16"),
    "SCL": ("SCL", "scl"),
}

REQUIRED_BANDS = tuple(BAND_ALIASES)

# WHY: bound the worst-case wait on a degraded backend so the search fails
#   over to the other backend instead of hanging.
_REQUEST_TIMEOUT_S = 60.0

# pystac-client surfaces timeouts and HTTP errors as APIError; the Planetary
# Computer signing calls use requests directly, hence RequestException.
_FALLBACK_ERRORS = (APIError, RequestException)


@dataclass(frozen=True)
class SceneMeta:
    """Lightweight scene metadata with canonical band asset hrefs."""

    id: str
    datetime: datetime
    source: str
    processing_baseline: str | None
    boa_offset_applied: bool
    assets: dict[str, str]
    # WHY: whole-tile eo:cloud_cover from the catalog — the series builder
    #   records it per scene (tile_cloud_pct). None when the catalog omits it
    #   or the scene was rebuilt from an old cache file without the tag.
    cloud_cover: float | None = None


def _baseline_key(value: str | None) -> tuple[int, tuple[int, ...]]:
    """Sort key for s2:processing_baseline ("05.11" > "04.00")."""
    try:
        return (1, tuple(int(part) for part in str(value).split(".")))
    except (TypeError, ValueError):
        return (0, ())


def deduplicate(items: list[pystac.Item]) -> list[pystac.Item]:
    """Dedup items of the same date and tile, keeping the highest baseline."""
    best: dict[tuple[str, str], pystac.Item] = {}
    for item in items:
        day = item.datetime.date().isoformat() if item.datetime else ""
        tile = str(item.properties.get("s2:mgrs_tile", item.id))
        key = (day, tile)
        current = best.get(key)
        baseline = _baseline_key(item.properties.get("s2:processing_baseline"))
        if current is None or baseline > _baseline_key(
            current.properties.get("s2:processing_baseline")
        ):
            best[key] = item
    return sorted(
        best.values(),
        key=lambda i: (i.datetime.isoformat() if i.datetime else "", i.id),
    )


def item_to_scene(item: pystac.Item, source: str) -> SceneMeta | None:
    """Convert an item to scene metadata, or None when bands are missing."""
    hrefs: dict[str, str] = {}
    for canonical, aliases in BAND_ALIASES.items():
        for alias in aliases:
            if alias in item.assets:
                hrefs[canonical] = item.assets[alias].href
                break
        else:
            logger.warning(
                "Skipping scene %s: missing asset for %s", item.id, canonical
            )
            return None
    if item.datetime is None:
        logger.warning("Skipping scene %s: missing datetime", item.id)
        return None
    cloud = item.properties.get("eo:cloud_cover")
    try:
        cloud_cover = None if cloud is None else float(cloud)
    except (TypeError, ValueError):
        cloud_cover = None
    return SceneMeta(
        id=item.id,
        datetime=item.datetime,
        source=source,
        processing_baseline=item.properties.get("s2:processing_baseline"),
        boa_offset_applied=bool(
            item.properties.get("earthsearch:boa_offset_applied", False)
        ),
        assets=hrefs,
        cloud_cover=cloud_cover,
    )


def _sign_item(item: pystac.Item) -> pystac.Item:
    """Sign Planetary Computer asset URLs (seam for tests to stub)."""
    return pc.sign(item)


def _search_backend(
    backend: str,
    start: date,
    end: date,
    bbox: tuple[float, float, float, float],
    max_cloud_cover: float,
) -> list[SceneMeta]:
    """Run the search against a single backend (may raise, caller falls back)."""
    config = _BACKENDS[backend]
    client = Client.open(str(config["url"]), timeout=_REQUEST_TIMEOUT_S)
    search = client.search(
        collections=[str(config["collection"])],
        bbox=list(bbox),
        datetime=f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        query={"eo:cloud_cover": {"lt": max_cloud_cover}},
    )
    scenes: list[SceneMeta] = []
    for item in deduplicate(list(search.items())):
        if config["signed"]:
            item = _sign_item(item)
        scene = item_to_scene(item, source=backend)
        if scene is not None:
            scenes.append(scene)
    return scenes


def get_scene(backend: str, item_id: str) -> SceneMeta:
    """Fetch one scene by exact backend + item id (no date search).

    Used by the identity gate, which re-ingests the reference series'
    Earth Search items one by one. Raises ValueError for an unknown
    backend, RuntimeError when the item is missing or lacks bands.
    """
    if backend not in _BACKENDS:
        msg = f"unknown STAC backend(s): {backend}"
        raise ValueError(msg)
    config = _BACKENDS[backend]
    client = Client.open(str(config["url"]), timeout=_REQUEST_TIMEOUT_S)
    item = client.get_collection(str(config["collection"])).get_item(item_id)
    if item is None:
        msg = f"item {item_id} not found on {backend}"
        raise RuntimeError(msg)
    if config["signed"]:
        item = _sign_item(item)
    scene = item_to_scene(item, source=backend)
    if scene is None:
        msg = f"item {item_id} on {backend} is missing required bands"
        raise RuntimeError(msg)
    logger.info("STAC item %s served by %s", item_id, backend)
    return scene


def search_scenes(
    start_date: date,
    end_date: date,
    max_cloud_cover: float,
    bbox: tuple[float, float, float, float] | None = None,
    backends: tuple[str, ...] = (PLANETARY_COMPUTER, EARTH_SEARCH),
) -> list[SceneMeta]:
    """Search scenes, falling back to the next backend on timeout/HTTP error.

    Dates are inclusive; bbox defaults to the AOI WGS84 bbox from aoi.py.
    """
    if end_date < start_date:
        msg = f"end_date {end_date} is before start_date {start_date}"
        raise ValueError(msg)
    unknown = [name for name in backends if name not in _BACKENDS]
    if unknown:
        msg = f"unknown STAC backend(s): {', '.join(unknown)}"
        raise ValueError(msg)
    if bbox is None:
        bbox = wgs84_bbox()
    last_error: Exception | None = None
    for backend in backends:
        try:
            scenes = _search_backend(
                backend, start_date, end_date, bbox, max_cloud_cover
            )
        except _FALLBACK_ERRORS as exc:
            logger.warning("STAC backend %s failed (%r); trying next", backend, exc)
            last_error = exc
            continue
        logger.info("STAC search served by %s: %d scene(s)", backend, len(scenes))
        return scenes
    msg = f"all STAC backends failed: {', '.join(backends)}"
    raise RuntimeError(msg) from last_error
