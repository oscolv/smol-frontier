"""Publish a versioned data contract derived from one completed series run.

``publish_run`` picks a run (``--run`` or the newest complete one), refuses
runs that cannot back a reproducible contract (incomplete, dirty, no scene
with figures), maps the run's scene records to the English contract fields,
validates every document against its packaged JSON Schema, re-classifies
the latest scene from the hash-verified scene cache into the published map
(classes 0-5, class 5 = removed by the exposed-bed correction), and swaps
the output directory in atomically. The run manifest is provenance, never
contract: nothing is copied out of it verbatim.

Contract files are canonical JSON (sorted keys, 2-space indent, UTF-8,
trailing newline) with no wall-clock timestamp, so the same run yields
byte-identical files; only the integrity index `publish.json` carries a timestamp.
"""

# WHY a package: one module per job (decision rule 3) — `select` picks the
#   run, `documents` maps it to the contract, `map` rebuilds the latest
#   scene's map, `writer` stages and swaps the output in. Every public name
#   stays importable from `smol_frontier.publish`.

from smol_frontier.config import settings as settings  # re-exported (tests read it)
from smol_frontier.publish.documents import (
    SCHEMA_VERSION,
    canonical_json,
    current_level,
    latest_document,
    latest_record,
    series_document,
    validate,
)
from smol_frontier.publish.documents import (
    _validator as _validator,  # re-exported (tests read it)
)
from smol_frontier.publish.errors import ContractInvalid, PublishRefused
from smol_frontier.publish.map import PublishedMap, build_map, map_path, png_path
from smol_frontier.publish.select import select_run
from smol_frontier.publish.writer import (
    ALLOWED_FILES,
    ALLOWED_MAP_PATTERN,
    INDEX_FILE,
    check_out_dir,
    is_allowed,
    publish_index,
    publish_run,
)

__all__ = [
    "ALLOWED_FILES",
    "ALLOWED_MAP_PATTERN",
    "INDEX_FILE",
    "SCHEMA_VERSION",
    "ContractInvalid",
    "PublishRefused",
    "PublishedMap",
    "build_map",
    "canonical_json",
    "check_out_dir",
    "current_level",
    "is_allowed",
    "latest_document",
    "latest_record",
    "map_path",
    "png_path",
    "publish_index",
    "publish_run",
    "select_run",
    "series_document",
    "validate",
]
