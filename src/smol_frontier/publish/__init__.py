"""Publish a versioned data contract derived from one completed series run.

``publish_run`` picks a run (``--run`` or the newest complete one), refuses
runs that cannot back a reproducible contract (incomplete, dirty, no scene
with figures), maps the run's scene records to the English contract fields,
validates every document against its packaged JSON Schema, re-classifies
the latest scene from the hash-verified scene cache into the published map
(classes 0-5, class 5 = removed by the exposed-bed correction), reads its
Sentinel-2 true-colour photo and the reservoir-clipped photos of the
``same_month`` scenes on the map's display grid, and swaps
the output directory in atomically. The run manifest is provenance, never
contract: nothing is copied out of it verbatim.

Contract files are canonical JSON (sorted keys, 2-space indent, UTF-8,
trailing newline) with no wall-clock timestamp, so the same run yields
byte-identical files; only the integrity index `publish.json` carries a timestamp.
"""

# WHY a package: one module per job (decision rule 3) — `select` picks the
#   run, `documents` maps it to the contract, `map` rebuilds the latest
#   scene's map, `truecolour` reads, clips and writes the photos, `writer` stages
#   and swaps the output in. Every public name
#   stays importable from `smol_frontier.publish`.

from smol_frontier.config import settings as settings  # re-exported (tests read it)
from smol_frontier.publish.documents import (
    CURVE_ANCHOR_SOURCE,
    SCHEMA_VERSION,
    canonical_json,
    current_level,
    latest_document,
    latest_record,
    reservoir_outline,
    series_document,
    stage_area_curve,
    validate,
)
from smol_frontier.publish.documents import (
    _validator as _validator,  # re-exported (tests read it)
)
from smol_frontier.publish.errors import ContractInvalid, PublishRefused
from smol_frontier.publish.map import (
    CommittedCurve,
    PublishedMap,
    build_map,
    committed_curve,
    map_path,
    png_path,
)
from smol_frontier.publish.select import select_run
from smol_frontier.publish.truecolour import guard_true_colour, image_path
from smol_frontier.publish.writer import (
    ALLOWED_FILES,
    ALLOWED_IMAGE_PATTERN,
    ALLOWED_MAP_PATTERN,
    INDEX_FILE,
    check_out_dir,
    is_allowed,
    publish_index,
    publish_run,
)

__all__ = [
    "ALLOWED_FILES",
    "ALLOWED_IMAGE_PATTERN",
    "ALLOWED_MAP_PATTERN",
    "CURVE_ANCHOR_SOURCE",
    "INDEX_FILE",
    "SCHEMA_VERSION",
    "CommittedCurve",
    "ContractInvalid",
    "PublishRefused",
    "PublishedMap",
    "build_map",
    "canonical_json",
    "check_out_dir",
    "committed_curve",
    "current_level",
    "guard_true_colour",
    "image_path",
    "is_allowed",
    "latest_document",
    "latest_record",
    "map_path",
    "png_path",
    "publish_index",
    "publish_run",
    "reservoir_outline",
    "select_run",
    "series_document",
    "stage_area_curve",
    "validate",
]
