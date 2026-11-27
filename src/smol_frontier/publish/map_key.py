"""The scene-map fingerprint shared by the map and viewer exports.

``map_key`` says whether a scene's published class map may have changed
between two runs, so ``export maps`` and ``export viewer`` agree on when a
map can be reused.
"""

import hashlib

from smol_frontier.publish.documents import canonical_json

__all__ = ["map_key"]

# WHY: the run inputs build_map reads besides the scene record (the cache
#   file is pinned by the record's cache_sha256); a change to any of them
#   may move the class map, so it is part of the map key.
_MAP_INPUTS = ("aoi_polygon", "flood_stage_tif", "stage_area_curve_csv")


def map_key(record: dict[str, object], manifest: dict[str, object]) -> str:
    """Fingerprint of everything a scene's class map is built from (pure).

    The scene record (cache sha256, run row) and the run inputs in
    ``_MAP_INPUTS``. Two runs with the same key, exported by the same smolf
    version and config, rebuild the same map, so its classified.webp can be
    reused.
    """
    inputs = manifest["inputs"]
    document = {
        "record": record,
        "inputs": {name: inputs.get(name) for name in _MAP_INPUTS},
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
