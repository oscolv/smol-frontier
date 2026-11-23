"""Export: downloadable files derived from a finished publish.

``export_files`` is pure: the parsed ``series.json`` and ``latest.json``
plus the ``series.json`` sha256 go in, ``{filename: bytes}`` comes out:
``series.csv`` (one row per scene), ``series_monthly.csv`` (the monthly
medians), ``README.txt`` and its Spanish counterpart ``LEEME.txt`` (licence,
disclaimer, citation, sources, provenance, column dictionary),
``reservoir_outline.zip`` (the AOI polygon as a shapefile) and
``SHA256SUMS`` over the other five.
``export_publish`` reads a publish directory, verifies it against its own
integrity index and the packaged schemas, and swaps the files into the
output directory by rename. It reads only the published contract and the
committed AOI polygon: no network, no scene cache, no run directory. The
exports are outside the contract; nothing here changes the published files
or their schemas.
"""

import csv
import hashlib
import io
import json
import shutil
import tempfile
import textwrap
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import NamedTuple

from smol_frontier import __version__, aoi, paths
from smol_frontier.publish.documents import validate
from smol_frontier.publish.errors import ContractInvalid, PublishRefused
from smol_frontier.publish.outline import OUTLINE_ZIP, outline_zip
from smol_frontier.publish.writer import (
    INDEX_FILE,
    _debris,
    _resolve_out_dir,
    _restore_previous,
    _staging_prefix,
    _swap_in,
)

# WHY: allowlist — the only names `export` may ever write, and the only
#   entries an existing ``--out`` may hold (anything else is someone else's
#   data and is refused, never replaced). One set so later exports extend it.
EXPORT_FILES = frozenset(
    {
        "series.csv",
        "series_monthly.csv",
        "README.txt",
        "LEEME.txt",
        OUTLINE_ZIP,
        "SHA256SUMS",
    }
)

# WHY: the files SHA256SUMS covers, in its (sorted) order; never itself.
_SUMMED_FILES = (
    "LEEME.txt",
    "README.txt",
    OUTLINE_ZIP,
    "series.csv",
    "series_monthly.csv",
)

# WHY: every row says which run and which published file it came from, so a
#   CSV copied out of its folder still traces back to the contract.
#   series_sha256 is the run's internal series.csv hash (the series id), not
#   the hash of this file; series_json_sha256 links it to series.json.
PROVENANCE_COLUMNS = (
    "run_id",
    "series_sha256",
    "series_json_sha256",
    "schema_version",
    "smolf_version",
)

# WHY: the licence travels with every row, so a CSV (or a row) copied out of
#   its folder without README.txt still says how it may be reused and whom
#   to credit. SPDX id for the licence; holder with affiliation.
RIGHTS_COLUMNS = ("license", "rights_holder")
_LICENCE_SPDX = "CC-BY-4.0"
_HOLDER = "Oscar Olvera Neria (UAM Azcapotzalco)"

# WHY: the per-scene fields in the order of the per-scene table of
#   docs/contract.md (tests check both the order and that the set equals the
#   packaged per-scene schema), with the display_ha object flattened in
#   place into its three integer parts so the CSV stays one value per cell.
SCENE_COLUMNS = (
    "date",
    "item_id",
    "quality_light",
    "regime",
    "valid_frac",
    "cloud_frac",
    "dense_ha",
    "interface_ha",
    "hyacinth_raw_ha",
    "hyacinth_under_haze_ha",
    "pct_reservoir_raw",
    "bed_applied",
    "bed_reason",
    "level_m",
    "removed_ha",
    "dense_corrected_ha",
    "interface_corrected_ha",
    "hyacinth_corrected_ha",
    "pct_reservoir_corrected",
    "cover_band",
    "display_status",
    "display_ha_total",
    "display_ha_dense",
    "display_ha_interface",
)

# WHY: the fields of a series.json ``monthly`` entry, in the contract's order.
MONTHLY_COLUMNS = ("month", "median_ha", "n_scenes")

# WHY: one header per CSV, provenance first, so both files trace back alike.
_SERIES_HEADER = (*PROVENANCE_COLUMNS, *RIGHTS_COLUMNS, *SCENE_COLUMNS)
_MONTHLY_HEADER = (*PROVENANCE_COLUMNS, *RIGHTS_COLUMNS, *MONTHLY_COLUMNS)

# WHY: the contract's display_ha parts, flattened as display_ha_<part>.
_DISPLAY_HA_PARTS = ("total", "dense", "interface")

# WHY: fixed decimals per numeric column, the contract's own rounding
#   (hectares, percentages and levels to 2, fractions to 4, display hectares
#   whole), so 15.7 prints 15.70 and every row lines up. Explicit per column,
#   never inferred from the value: a JSON 0 and 0.0 must print alike.
_DECIMALS = {
    "valid_frac": 4,
    "cloud_frac": 4,
    "dense_ha": 2,
    "interface_ha": 2,
    "hyacinth_raw_ha": 2,
    "hyacinth_under_haze_ha": 2,
    "pct_reservoir_raw": 2,
    "level_m": 2,
    "removed_ha": 2,
    "dense_corrected_ha": 2,
    "interface_corrected_ha": 2,
    "hyacinth_corrected_ha": 2,
    "pct_reservoir_corrected": 2,
    "display_ha_total": 0,
    "display_ha_dense": 0,
    "display_ha_interface": 0,
    "median_ha": 2,
}


class ColumnDoc(NamedTuple):
    """One README dictionary entry: unit, meaning and when the cell is empty."""

    unit: str
    meaning: str
    empty_when: str


_POOR = "POOR scene"
_POOR_OR_NO_BED = "POOR scene, or bed_applied false"

# WHY: the README's column dictionary, one entry per column of either CSV
#   (a test fails on a missing or extra entry). Meanings follow the per-scene
#   and monthly tables of docs/contract.md, worded for a reader who has only
#   the downloaded folder.
COLUMN_DICTIONARY: dict[str, ColumnDoc] = {
    "run_id": ColumnDoc("none", "Id of the series run the row comes from.", "never"),
    "series_sha256": ColumnDoc(
        "none",
        "sha256 of the run's internal series.csv (the series id); not the hash "
        "of the series.csv in this folder.",
        "never",
    ),
    "series_json_sha256": ColumnDoc(
        "none", "sha256 of the published series.json this file was made from.", "never"
    ),
    "schema_version": ColumnDoc(
        "none", "Contract schema version of that series.json.", "never"
    ),
    "smolf_version": ColumnDoc(
        "none",
        "smolf version that published that series.json (not the exporter).",
        "never",
    ),
    "license": ColumnDoc(
        "none",
        "SPDX id of the data licence (CC BY 4.0); see README.txt.",
        "never",
    ),
    "rights_holder": ColumnDoc(
        "none", "Copyright holder of the data, to credit on reuse.", "never"
    ),
    "date": ColumnDoc("YYYY-MM-DD", "UTC acquisition date.", "never"),
    "item_id": ColumnDoc("none", "Exact STAC item id of the scene used.", "never"),
    "quality_light": ColumnDoc(
        "none", "Scene quality light: GOOD, DOUBTFUL or POOR.", "never"
    ),
    "regime": ColumnDoc(
        "none", "Optical regime of the scene: CLEAN, EXPOSED_BED or TURBIDITY.", _POOR
    ),
    "valid_frac": ColumnDoc(
        "fraction 0-1",
        "Visible (cloud-free, observed) fraction of the reservoir.",
        "never",
    ),
    "cloud_frac": ColumnDoc(
        "fraction 0-1", "Clouded fraction of the reservoir.", _POOR
    ),
    "dense_ha": ColumnDoc("ha", "Dense hyacinth, raw.", _POOR),
    "interface_ha": ColumnDoc(
        "ha", "Interface hyacinth (mat edge / sparse cover), raw.", _POOR
    ),
    "hyacinth_raw_ha": ColumnDoc(
        "ha", "Dense + interface, before the exposed-bed correction.", _POOR
    ),
    "hyacinth_under_haze_ha": ColumnDoc(
        "ha",
        "Part of hyacinth_raw_ha seen through thin haze inside the day's water "
        "mirror and rescued by the classifier's second pass; raw. 0 when nothing "
        "was rescued or the exposed-bed correction was not applied.",
        _POOR,
    ),
    "pct_reservoir_raw": ColumnDoc(
        "% 0-100", "hyacinth_raw_ha over the reservoir area.", _POOR
    ),
    "bed_applied": ColumnDoc(
        "true/false", "Whether the exposed-bed correction was applied.", _POOR
    ),
    "bed_reason": ColumnDoc(
        "none",
        "Why the correction was not applied (e.g. no SIH level for the day).",
        "POOR scene, or the correction was applied",
    ),
    "level_m": ColumnDoc(
        "m a.s.l.",
        "Water level of the scene day used by the correction.",
        "POOR scene, or no level for the day",
    ),
    "removed_ha": ColumnDoc(
        "ha",
        "Hyacinth removed by the correction (on the exposed bed).",
        _POOR_OR_NO_BED,
    ),
    "dense_corrected_ha": ColumnDoc(
        "ha", "Dense hyacinth after the correction.", _POOR_OR_NO_BED
    ),
    "interface_corrected_ha": ColumnDoc(
        "ha", "Interface hyacinth after the correction.", _POOR_OR_NO_BED
    ),
    "hyacinth_corrected_ha": ColumnDoc(
        "ha",
        "Total hyacinth after the correction; the public figure.",
        _POOR_OR_NO_BED,
    ),
    "pct_reservoir_corrected": ColumnDoc(
        "% 0-100",
        "hyacinth_corrected_ha over the reservoir area; the public percentage.",
        _POOR_OR_NO_BED,
    ),
    "cover_band": ColumnDoc(
        "none",
        "Management band of pct_reservoir_corrected: GREEN below green_max_pct, "
        "YELLOW from it to below red_min_pct, RED at or above red_min_pct.",
        _POOR_OR_NO_BED,
    ),
    "display_status": ColumnDoc(
        "none",
        "Display gate: HIDDEN when quality_light is not GOOD or "
        "hyacinth_corrected_ha is empty; LOWER_BOUND when valid_frac is below "
        "frac_publishable (read the figures as 'at least'); FULL otherwise.",
        "never",
    ),
    "display_ha_total": ColumnDoc(
        "ha (whole)", "hyacinth_corrected_ha rounded half up.", _POOR_OR_NO_BED
    ),
    "display_ha_dense": ColumnDoc(
        "ha (whole)",
        "dense_corrected_ha apportioned to whole hectares.",
        _POOR_OR_NO_BED,
    ),
    "display_ha_interface": ColumnDoc(
        "ha (whole)",
        "interface_corrected_ha apportioned to whole hectares; "
        "display_ha_dense + display_ha_interface = display_ha_total.",
        _POOR_OR_NO_BED,
    ),
    "month": ColumnDoc(
        "YYYY-MM",
        "Calendar month (UTC dates); every month from the first to the last "
        "scene month, none skipped.",
        "never",
    ),
    "median_ha": ColumnDoc(
        "ha",
        "Ordinary median (mean of the two middle values when the count is even) "
        "of the month's hyacinth_corrected_ha over its FULL and LOWER_BOUND "
        "scenes.",
        "n_scenes is 0",
    ),
    "n_scenes": ColumnDoc(
        "count",
        "The month's scenes with display_status FULL or LOWER_BOUND.",
        "never",
    ),
}

# WHY: the README's fixed texts. Copyright holder and licence of the data
#   (the code stays MIT), worded like tests/reference/LICENSE.
_TITLE = "Water hyacinth on Endho Reservoir: Sentinel-2 series"
_LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"
_REPOSITORY = "https://github.com/oscolv/smol-frontier"
# WHY 79: plain text read in a terminal or a viewer without wrapping.
_WIDTH = 79

# WHY: the two published documents the export reads; maps and photos never.
_SOURCES = ("series.json", "latest.json")


def _cell(column: str, value: object) -> str:
    """One CSV cell: null empty, booleans lowercase, numbers fixed decimals."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    decimals = _DECIMALS.get(column)
    if decimals is None:
        return str(value)
    if decimals == 0:
        return f"{value:d}"
    return f"{value:.{decimals}f}"


def _scene_values(scene: dict[str, object]) -> dict[str, object]:
    """A scene entry keyed by CSV column (display_ha flattened)."""
    values = {key: value for key, value in scene.items() if key != "display_ha"}
    display = scene["display_ha"]
    for part in _DISPLAY_HA_PARTS:
        values[f"display_ha_{part}"] = None if display is None else display[part]
    return values


def _csv_bytes(header: tuple[str, ...], rows: list[list[str]]) -> bytes:
    """UTF-8 (no BOM), comma, LF, minimal RFC 4180 quoting."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _require_same_run(
    series_doc: dict[str, object], other: dict[str, object], what: str
) -> None:
    """Refuse ``other`` unless it names the run and series of ``series_doc``."""
    for field in ("run_id", "series_sha256"):
        if other[field] != series_doc[field]:
            msg = (
                f"{what} does not belong to the run of series.json "
                f"({field}: {other[field]} != {series_doc[field]})"
            )
            raise PublishRefused(msg)


def _provenance(
    series_doc: dict[str, object], series_json_sha256: str
) -> dict[str, object]:
    """The provenance and rights cells every row of both CSVs carries."""
    return {
        "run_id": series_doc["run_id"],
        "series_sha256": series_doc["series_sha256"],
        "series_json_sha256": series_json_sha256,
        "schema_version": series_doc["schema_version"],
        "smolf_version": series_doc["smolf_version"],
        "license": _LICENCE_SPDX,
        "rights_holder": _HOLDER,
    }


def _table(
    header: tuple[str, ...],
    provenance: dict[str, object],
    entries: list[dict[str, object]],
) -> bytes:
    """A CSV of ``entries`` under ``header``, provenance on every row."""
    rows = []
    for entry in entries:
        values = {**provenance, **entry}
        rows.append([_cell(column, values[column]) for column in header])
    return _csv_bytes(header, rows)


def _section(title: str, body: str) -> str:
    """A README section: the title underlined with '=', then its body."""
    return f"{title}\n{'=' * len(title)}\n\n{body.rstrip()}\n"


def _wrap(text: str, indent: str = "", hanging: str | None = None) -> str:
    """``text`` filled to the README width; ``hanging`` indents later lines."""
    rest = indent if hanging is None else hanging
    # WHY no hyphen breaks: a URL split at "smol-" no longer works when copied.
    return textwrap.fill(
        text,
        _WIDTH,
        initial_indent=indent,
        subsequent_indent=rest,
        break_on_hyphens=False,
    )


def _years(series_doc: dict[str, object]) -> str:
    """The span of scene years, ``2017-2026`` (or one year)."""
    years = sorted({s["date"][:4] for s in series_doc["scenes"]})
    return years[0] if len(years) == 1 else f"{years[0]}-{years[-1]}"


def _dictionary(
    name: str, what: str, columns: tuple[str, ...], text: "ReadmeText"
) -> str:
    """One CSV's dictionary block: heading, then unit, empty rule and meaning."""
    heading = f"{name} ({what})"
    entries = [f"{heading}\n{'-' * len(heading)}"]
    for column in columns:
        doc = text.dictionary[column]
        unit, empty = text.unit_label, text.empty_label
        entries.append(
            f"  {column}\n"
            + _wrap(f"{unit}: {doc.unit}. {empty}: {doc.empty_when}.", " " * 6)
            + "\n"
            + _wrap(doc.meaning, " " * 6)
        )
    return "\n\n".join(entries)


class ReadmeText(NamedTuple):
    """The fixed texts of one README language; one builder lays them out.

    ``{year}``, ``{holder}`` and ``{years}`` are filled in by ``_readme``.
    """

    filename: str
    encoding: str
    sections: tuple[str, ...]  # title, licence, ..., full definitions (8)
    description: str
    licence_head: str
    licence_urls: tuple[str, ...]
    licence_body: tuple[str, ...]
    disclaimer: str
    citation_intro: str
    sources: tuple[str, ...]
    provenance_note: tuple[str, str]
    labels: dict[str, str]  # contract_version, exported, constants
    dictionary: dict[str, ColumnDoc]
    tables: tuple[str, str]  # what one row of series.csv / series_monthly.csv is
    unit_label: str
    empty_label: str
    pointer: str


# WHY: the English README. ASCII only, so it reads anywhere.
README_EN = ReadmeText(
    filename="README.txt",
    encoding="ascii",
    sections=(
        _TITLE,
        "Licence",
        "Disclaimer",
        "Suggested citation",
        "Sources",
        "Provenance",
        "Column dictionary",
        "Full definitions",
    ),
    description=(
        "Floating water hyacinth cover on Endho Reservoir (Hidalgo, Mexico), "
        "classified from Sentinel-2 L2A scenes by smolf, with the exposed-bed "
        "correction behind the public figure. series.csv has one row per scene "
        "of the run (POOR scenes included, with empty figure cells); "
        "series_monthly.csv has the monthly median of the corrected hectares; "
        "reservoir_outline.zip is the reservoir polygon as a shapefile, with "
        "its own README. To check the files, run `sha256sum -c SHA256SUMS` "
        "in this folder. "
        "README.txt (English) and LEEME.txt (Spanish) describe the same data; "
        "if they differ, LEEME.txt prevails."
    ),
    licence_head=(
        "(C) {year} {holder}. Released under the Creative Commons Attribution "
        "4.0 International license (CC BY 4.0):"
    ),
    licence_urls=(_LICENCE_URL,),
    licence_body=(
        (
            "You may share and adapt this data, including for commercial purposes, "
            "as long as you give appropriate credit. When you publish results "
            "derived from it, please cite the series as suggested below."
        ),
        (
            "This license covers only the data in this folder. The smolf code that "
            "produced it stays under the MIT License."
        ),
    ),
    disclaimer=(
        "These figures are remote-sensing estimates computed by smolf from "
        "satellite imagery and published water levels. They are not an "
        "official measurement and do not replace data from CONAGUA or other "
        "authorities. The data is provided as is, without warranty of any "
        "kind (CC BY 4.0, section 5)."
    ),
    citation_intro="",
    sources=(
        "Contains modified Copernicus Sentinel data {years} (Sentinel-2 L2A).",
        (
            "Water levels: Mexico's national hydrometric information system (SIH), "
            "station ENDHD."
        ),
        (
            "Reservoir polygon: JRC Global Surface Water v1.4 (occurrence >= 25%, "
            "1984-2021; Pekel et al., 2016)."
        ),
    ),
    provenance_note=(
        "The hash of the run's internal series.csv (the series id),",
        "not the hash of the series.csv in this folder.",
    ),
    labels={
        "contract_version": "contract smolf_version",
        "exported": "exported by smolf",
        "constants": "constants",
    },
    dictionary=COLUMN_DICTIONARY,
    tables=("one row per scene", "one row per month"),
    unit_label="Unit",
    empty_label="Empty when",
    pointer=(
        "The full field definitions, with the quality gates and the rounding "
        f"rules, are in docs/contract.md of the smolf repository ({_REPOSITORY})."
        " These files are derived from the contract but are not part of it."
    ),
)

# --- Spanish text of the published bundle ------------------------------------
# WHY Spanish here: LEEME.txt is a deliverable read by Spanish-speaking
#   residents, authorities and researchers; it is the one exception to the
#   English-only rule (AGENTS.md, Language; docs/decisions.md 19). Column
#   names and codes stay verbatim and are glossed, so both languages describe
#   byte-identical CSVs. The licence is not translated here: it links
#   Creative Commons' official Spanish deed and legal code.

_POOR_ES = "escena POOR"
_POOR_OR_NO_BED_ES = "escena POOR, o bed_applied false"
_NONE_ES = "sin unidad"
_NEVER_ES = "nunca"

COLUMN_DICTIONARY_ES: dict[str, ColumnDoc] = {
    "run_id": ColumnDoc(
        _NONE_ES, "Id de la corrida de la serie de la que viene la fila.", _NEVER_ES
    ),
    "series_sha256": ColumnDoc(
        _NONE_ES,
        "sha256 del series.csv interno de la corrida (el id de la serie); no es "
        "el hash del series.csv de esta carpeta.",
        _NEVER_ES,
    ),
    "series_json_sha256": ColumnDoc(
        _NONE_ES,
        "sha256 del series.json publicado del que se generó este archivo.",
        _NEVER_ES,
    ),
    "schema_version": ColumnDoc(
        _NONE_ES, "Versión del esquema del contrato de ese series.json.", _NEVER_ES
    ),
    "smolf_version": ColumnDoc(
        _NONE_ES,
        "Versión de smolf que publicó ese series.json (no la que exportó).",
        _NEVER_ES,
    ),
    "license": ColumnDoc(
        _NONE_ES,
        "Id SPDX de la licencia de los datos (CC BY 4.0); ver LEEME.txt.",
        _NEVER_ES,
    ),
    "rights_holder": ColumnDoc(
        _NONE_ES,
        "Titular de los derechos de los datos, a quien dar crédito al reutilizarlos.",
        _NEVER_ES,
    ),
    "date": ColumnDoc("AAAA-MM-DD", "Fecha de adquisición (UTC).", _NEVER_ES),
    "item_id": ColumnDoc(
        _NONE_ES, "Id exacto del ítem STAC de la escena usada.", _NEVER_ES
    ),
    "quality_light": ColumnDoc(
        _NONE_ES,
        "Semáforo de calidad de la escena: GOOD = BUENA, DOUBTFUL = DUDOSA, "
        "POOR = MALA.",
        _NEVER_ES,
    ),
    "regime": ColumnDoc(
        _NONE_ES,
        "Régimen óptico de la escena: CLEAN = limpio, EXPOSED_BED = lecho "
        "expuesto, TURBIDITY = turbidez.",
        _POOR_ES,
    ),
    "valid_frac": ColumnDoc(
        "fracción 0-1",
        "Fracción visible (sin nubes, observada) del vaso.",
        _NEVER_ES,
    ),
    "cloud_frac": ColumnDoc("fracción 0-1", "Fracción del vaso con nubes.", _POOR_ES),
    "dense_ha": ColumnDoc("ha", "Lirio denso, sin corregir.", _POOR_ES),
    "interface_ha": ColumnDoc(
        "ha",
        "Lirio de interfase (borde del manto / cobertura rala), sin corregir.",
        _POOR_ES,
    ),
    "hyacinth_raw_ha": ColumnDoc(
        "ha", "Denso + interfase, antes de la corrección por lecho expuesto.", _POOR_ES
    ),
    "hyacinth_under_haze_ha": ColumnDoc(
        "ha",
        "Parte de hyacinth_raw_ha vista a través de neblina delgada dentro del "
        "espejo de agua del día y rescatada por la segunda pasada del "
        "clasificador; sin corregir. 0 cuando no se rescató nada o no se aplicó "
        "la corrección por lecho expuesto.",
        _POOR_ES,
    ),
    "pct_reservoir_raw": ColumnDoc(
        "% 0-100", "hyacinth_raw_ha sobre el área del vaso.", _POOR_ES
    ),
    "bed_applied": ColumnDoc(
        "true/false",
        "Si se aplicó la corrección por lecho expuesto (true = sí, false = no).",
        _POOR_ES,
    ),
    "bed_reason": ColumnDoc(
        _NONE_ES,
        "Por qué no se aplicó la corrección, en inglés (p. ej., no hay cota del "
        "SIH para el día).",
        "escena POOR, o se aplicó la corrección",
    ),
    "level_m": ColumnDoc(
        "m s. n. m.",
        "Cota del agua del día de la escena usada por la corrección.",
        "escena POOR, o no hay cota para el día",
    ),
    "removed_ha": ColumnDoc(
        "ha",
        "Lirio retirado por la corrección (sobre el lecho expuesto).",
        _POOR_OR_NO_BED_ES,
    ),
    "dense_corrected_ha": ColumnDoc(
        "ha", "Lirio denso después de la corrección.", _POOR_OR_NO_BED_ES
    ),
    "interface_corrected_ha": ColumnDoc(
        "ha", "Lirio de interfase después de la corrección.", _POOR_OR_NO_BED_ES
    ),
    "hyacinth_corrected_ha": ColumnDoc(
        "ha",
        "Lirio total después de la corrección; la cifra pública.",
        _POOR_OR_NO_BED_ES,
    ),
    "pct_reservoir_corrected": ColumnDoc(
        "% 0-100",
        "hyacinth_corrected_ha sobre el área del vaso; el porcentaje público.",
        _POOR_OR_NO_BED_ES,
    ),
    "cover_band": ColumnDoc(
        _NONE_ES,
        "Semáforo de manejo de pct_reservoir_corrected: GREEN = verde (debajo "
        "de green_max_pct), YELLOW = amarillo (desde ahí hasta debajo de "
        "red_min_pct), RED = rojo (en red_min_pct o más).",
        _POOR_OR_NO_BED_ES,
    ),
    "display_status": ColumnDoc(
        _NONE_ES,
        "Filtro de despliegue: HIDDEN = oculta, cuando quality_light no es GOOD "
        "o hyacinth_corrected_ha está vacía; LOWER_BOUND = cota inferior, "
        "cuando valid_frac es menor que frac_publishable (leer las cifras como "
        "'al menos'); FULL = completa en otro caso.",
        _NEVER_ES,
    ),
    "display_ha_total": ColumnDoc(
        "ha (enteras)",
        "hyacinth_corrected_ha redondeado (la mitad hacia arriba).",
        _POOR_OR_NO_BED_ES,
    ),
    "display_ha_dense": ColumnDoc(
        "ha (enteras)",
        "dense_corrected_ha repartido en hectáreas enteras.",
        _POOR_OR_NO_BED_ES,
    ),
    "display_ha_interface": ColumnDoc(
        "ha (enteras)",
        "interface_corrected_ha repartido en hectáreas enteras; "
        "display_ha_dense + display_ha_interface = display_ha_total.",
        _POOR_OR_NO_BED_ES,
    ),
    "month": ColumnDoc(
        "AAAA-MM",
        "Mes calendario (fechas UTC); todos los meses desde el de la primera "
        "escena hasta el de la última, sin saltos.",
        _NEVER_ES,
    ),
    "median_ha": ColumnDoc(
        "ha",
        "Mediana ordinaria (promedio de los dos valores centrales cuando el "
        "número es par) de hyacinth_corrected_ha de las escenas FULL y "
        "LOWER_BOUND del mes.",
        "n_scenes es 0",
    ),
    "n_scenes": ColumnDoc(
        "conteo",
        "Escenas del mes con display_status FULL o LOWER_BOUND.",
        _NEVER_ES,
    ),
}

README_ES = ReadmeText(
    filename="LEEME.txt",
    encoding="utf-8",
    sections=(
        "Lirio acuático en la presa Endhó: serie Sentinel-2",
        "Licencia",
        "Descargo de responsabilidad",
        "Cita sugerida",
        "Fuentes",
        "Procedencia",
        "Diccionario de columnas",
        "Definiciones completas",
    ),
    description=(
        "Cobertura de lirio acuático flotante en la presa Endhó (Hidalgo, "
        "México), clasificada por smolf a partir de escenas Sentinel-2 L2A, con "
        "la corrección por lecho expuesto detrás de la cifra pública. "
        "series.csv tiene una fila por escena de la corrida (incluidas las "
        "escenas POOR, con las celdas de cifras vacías); series_monthly.csv "
        "tiene la mediana mensual de las hectáreas corregidas; "
        "reservoir_outline.zip es el polígono del vaso como shapefile, con su "
        "propio README (en inglés). Para verificar "
        "los archivos, ejecute `sha256sum -c SHA256SUMS` en esta carpeta. "
        "Los nombres de columna y los códigos (GOOD, EXPOSED_BED, ...) están en "
        "inglés en los CSV; el diccionario de abajo los explica. LEEME.txt "
        "(español) y README.txt (inglés) describen los mismos datos; si "
        "difieren, prevalece LEEME.txt."
    ),
    licence_head=(
        "(C) {year} {holder}. Publicado bajo la licencia Creative Commons "
        "Atribución 4.0 Internacional (CC BY 4.0). Resumen y texto legal "
        "oficiales en español:"
    ),
    licence_urls=(_LICENCE_URL + "deed.es", _LICENCE_URL + "legalcode.es"),
    licence_body=(
        (
            "Puede compartir y adaptar estos datos, incluso con fines comerciales, "
            "siempre que dé el crédito correspondiente. Si publica resultados "
            "derivados de ellos, cite la serie como se sugiere abajo."
        ),
        (
            "Esta licencia cubre solo los datos de esta carpeta. El código de smolf "
            "que los produjo sigue bajo la licencia MIT."
        ),
    ),
    disclaimer=(
        "Estas cifras son estimaciones por percepción remota calculadas por "
        "smolf a partir de imágenes de satélite y de niveles de agua "
        "publicados. No son una medición oficial ni sustituyen los datos de la "
        "CONAGUA u otras autoridades. Los datos se ofrecen tal cual, sin "
        "garantía de ningún tipo (CC BY 4.0, sección 5)."
    ),
    citation_intro=(
        "La cita es la misma en README.txt, para que una cifra citada en "
        "cualquiera de los dos idiomas remita a la misma corrida:"
    ),
    sources=(
        "Contiene datos modificados de Copernicus Sentinel {years} (Sentinel-2 L2A).",
        (
            "Niveles de agua: Sistema de Información Hidrológica (SIH) de la "
            "CONAGUA, estación ENDHD."
        ),
        (
            "Polígono del vaso: JRC Global Surface Water v1.4 (ocurrencia >= 25 %, "
            "1984-2021; Pekel et al., 2016)."
        ),
    ),
    provenance_note=(
        "El hash del series.csv interno de la corrida (el id de la serie),",
        "no el hash del series.csv de esta carpeta.",
    ),
    labels={
        "contract_version": "smolf_version del contrato",
        "exported": "exportado por smolf",
        "constants": "constantes",
    },
    dictionary=COLUMN_DICTIONARY_ES,
    tables=("una fila por escena", "una fila por mes"),
    unit_label="Unidad",
    empty_label="Vacía cuando",
    pointer=(
        "Las definiciones completas de los campos, con los filtros de calidad "
        "y las reglas de redondeo, están en docs/contract.md del repositorio "
        f"de smolf ({_REPOSITORY}), en inglés. Estos archivos se derivan del "
        "contrato pero no forman parte de él."
    ),
)


def _readme(
    series_doc: dict[str, object],
    latest_doc: dict[str, object],
    series_json_sha256: str,
    text: ReadmeText,
) -> bytes:
    """One README (README.txt or LEEME.txt) from its language's texts.

    WHY the year from the latest scene: no wall clock, so the same publish
    always gives the same bytes. WHY one citation string for both languages:
    a figure cited in either traces to the same run.
    """
    run_id = series_doc["run_id"]
    year = latest_doc["date"][:4]
    licence = "\n\n".join(
        [
            _wrap(text.licence_head.format(year=year, holder=_HOLDER)),
            "\n".join(f"    {url}" for url in text.licence_urls),
            *(_wrap(paragraph) for paragraph in text.licence_body),
        ]
    )
    citation = _wrap(
        f"{_HOLDER} ({year}). {_TITLE}, run {run_id}. smolf. {_REPOSITORY}"
    )
    if text.citation_intro:
        citation = _wrap(text.citation_intro) + "\n\n" + citation
    years = _years(series_doc)
    sources = "\n".join(
        _wrap(line.format(years=years), "- ", "  ") for line in text.sources
    )
    constants = latest_doc["constants"]
    labels = text.labels
    provenance = "\n".join(
        [
            f"run_id: {run_id}",
            f"series_sha256: {series_doc['series_sha256']}",
            *(f"  {line}" for line in text.provenance_note),
            f"series.json sha256: {series_json_sha256}",
            f"schema_version: {series_doc['schema_version']}",
            f"{labels['contract_version']}: {series_doc['smolf_version']}",
            f"{labels['exported']}: {__version__}",
            f"reservoir_area_ha: {latest_doc['reservoir_area_ha']:.2f}",
            f"{labels['constants']}:",
            *(f"  {key}: {constants[key]}" for key in sorted(constants)),
        ]
    )
    dictionary = "\n\n".join(
        [
            _dictionary("series.csv", text.tables[0], _SERIES_HEADER, text),
            _dictionary("series_monthly.csv", text.tables[1], _MONTHLY_HEADER, text),
        ]
    )
    bodies = [
        _wrap(text.description),
        licence,
        _wrap(text.disclaimer),
        citation,
        sources,
        provenance,
        dictionary,
        _wrap(text.pointer),
    ]
    sections = [
        _section(title, body) for title, body in zip(text.sections, bodies, strict=True)
    ]
    return "\n".join(sections).encode(text.encoding)


def _outline(latest_doc: dict[str, object]) -> bytes:
    """The zipped AOI, refused unless it is the polygon the publish used.

    WHY the area check: the zip is read from the committed AOI at export
    time, so a polygon changed after the publish would ship an outline that
    disagrees with every published percentage.
    """
    area = round(aoi.reservoir_area_ha(), 2)
    if area != latest_doc["reservoir_area_ha"]:
        msg = (
            f"the AOI polygon ({area} ha) is not the one latest.json was "
            f"published with ({latest_doc['reservoir_area_ha']} ha)"
        )
        raise PublishRefused(msg)
    day = date.fromisoformat(str(latest_doc["date"]))
    return outline_zip(day, _HOLDER, _LICENCE_URL)


def _sha256sums(files: dict[str, bytes]) -> bytes:
    """``sha256sum -c`` lines (``<hex>  <name>``) for the summed files."""
    lines = [
        f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n" for name in _SUMMED_FILES
    ]
    return "".join(lines).encode("ascii")


def export_files(
    series_doc: dict[str, object],
    latest_doc: dict[str, object],
    series_json_sha256: str,
) -> dict[str, bytes]:
    """The export files, by name, of one publish's parsed documents.

    ``series.csv`` has one row per ``series.json`` scene, in its order, POOR
    scenes included; ``series_monthly.csv`` one row per ``monthly`` entry.
    Both carry the provenance columns. ``latest_doc`` supplies only the
    README's year, ``reservoir_area_ha`` and ``constants``, and dates the
    outline zip. Raises PublishRefused when ``latest_doc`` belongs to
    another run, or when the committed AOI is not the polygon it was
    published with. Deterministic: no clock.
    """
    _require_same_run(series_doc, latest_doc, "latest.json")
    provenance = _provenance(series_doc, series_json_sha256)
    scenes = [_scene_values(scene) for scene in series_doc["scenes"]]
    files = {
        "series.csv": _table(_SERIES_HEADER, provenance, scenes),
        "series_monthly.csv": _table(
            _MONTHLY_HEADER, provenance, series_doc["monthly"]
        ),
    }
    for text in (README_EN, README_ES):
        files[text.filename] = _readme(series_doc, latest_doc, series_json_sha256, text)
    files[OUTLINE_ZIP] = _outline(latest_doc)
    files["SHA256SUMS"] = _sha256sums(files)
    return files


def _read_json(path: Path) -> tuple[bytes, object]:
    """The bytes and parsed JSON of ``path``; PublishRefused if unreadable."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"cannot read {path}: {exc.strerror or exc}"
        raise PublishRefused(msg) from exc
    try:
        return data, json.loads(data)
    except ValueError as exc:
        msg = f"{path} is not valid JSON: {exc}"
        raise PublishRefused(msg) from exc


def _validated(name: str, document: object) -> dict[str, object]:
    """``document`` if it passes the packaged ``name`` schema, else refused."""
    try:
        validate(name.removesuffix(".json"), document)
    except ContractInvalid as exc:
        raise PublishRefused(str(exc)) from exc
    return document


def read_publish(
    from_dir: Path,
) -> tuple[dict[str, object], dict[str, object], str]:
    """``series.json``, ``latest.json`` and the series.json sha256, verified.

    ``publish.json`` must exist and pass its schema; each document must hash
    to its ``publish.json`` entry, pass its schema and name the index's run.
    Every failure is a PublishRefused.
    """
    _, index = _read_json(from_dir / INDEX_FILE)
    index = _validated(INDEX_FILE, index)
    expected = {entry["path"]: entry["sha256"] for entry in index["files"]}
    documents: dict[str, dict[str, object]] = {}
    hashes: dict[str, str] = {}
    for name in _SOURCES:
        if name not in expected:
            msg = f"{from_dir / INDEX_FILE} does not list {name}"
            raise PublishRefused(msg)
        data, document = _read_json(from_dir / name)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected[name]:
            msg = (
                f"{from_dir / name} does not match its {INDEX_FILE} entry "
                f"(sha256 {digest} != {expected[name]})"
            )
            raise PublishRefused(msg)
        documents[name] = _validated(name, document)
        hashes[name] = digest
    _require_same_run(documents["series.json"], index, INDEX_FILE)
    return documents["series.json"], documents["latest.json"], hashes["series.json"]


def _check_export_dir(out_dir: Path, given: Path, owns: Callable[[str], bool]) -> None:
    """Refuse an ``out_dir`` holding anything the export does not own.

    WHY: the swap renames ``out_dir`` away and deletes it, so it may only
    hold earlier exports: a missing or empty directory, or regular files
    ``owns`` accepts. A file, a symlink or a subdirectory is someone else's.
    """
    if not out_dir.exists():
        return
    shown = given if given.absolute() == out_dir else f"{given} (-> {out_dir})"
    if not out_dir.is_dir():
        msg = f"--out {shown} exists and is not a directory"
        raise PublishRefused(msg)
    for entry in sorted(out_dir.iterdir()):
        owned = owns(entry.name) and entry.is_file()
        if not owned or entry.is_symlink():
            msg = f"--out {shown} holds a file the export does not own: {entry.name}"
            raise PublishRefused(msg)


def _drop_stale_staging(out_dir: Path, owns: Callable[[str], bool]) -> None:
    """Remove staging dirs a crashed export left, if they hold only its files."""
    for staging in _debris(out_dir, "staging"):
        if all(owns(entry.name) for entry in staging.iterdir()):
            shutil.rmtree(staging, ignore_errors=True)


def write_export(
    out_dir: Path,
    files: dict[str, bytes],
    given: Path,
    owns: Callable[[str], bool] = EXPORT_FILES.__contains__,
) -> None:
    """Write ``files`` into a sibling staging dir, then swap it in.

    ``owns`` is the export's allowlist: every file written, and every entry
    an existing ``out_dir`` holds, must pass it.
    """
    for name in files:
        if not owns(name):
            msg = f"refusing to export a file outside the allowlist: {name}"
            raise ValueError(msg)
    # WHY this order, as publish's ``_recover_out_dir`` + ``check_out_dir``:
    #   a lone ``.previous-*`` must be back before the check, or a crash
    #   mid-swap would read as a missing ``--out`` and lose the old export
    #   (restoring only renames, so a refusal after it loses nothing). The
    #   refusal checks then run before anything is deleted: stale staging is
    #   dropped only once ``--out`` is known to be the export's own.
    _restore_previous(out_dir)
    _check_export_dir(out_dir, given, owns)
    _drop_stale_staging(out_dir, owns)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=_staging_prefix(out_dir.name), dir=out_dir.parent)
    )
    # WHY: mkdtemp creates a private (0700) dir; downloads are for others.
    staging.chmod(0o755)
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        _swap_in(staging, out_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def export_publish(from_dir: Path | None = None, out_dir: Path | None = None) -> Path:
    """Export a verified publish into ``out_dir``; return ``out_dir`` as given.

    Defaults resolve at call time: ``paths.publish_dir()`` and
    ``paths.downloads_dir()``. Every input check runs before anything is
    written; a refusal (PublishRefused, exit 2) leaves ``out_dir`` as it was.
    """
    source = paths.publish_dir() if from_dir is None else Path(from_dir)
    target = paths.downloads_dir() if out_dir is None else Path(out_dir)
    series_doc, latest_doc, series_json_sha256 = read_publish(source)
    files = export_files(series_doc, latest_doc, series_json_sha256)
    real = resolve_export_dir(source, target)
    write_export(real, files, target)
    return target


def resolve_export_dir(source: Path, target: Path) -> Path:
    """The real ``--out`` path, refused when it is inside the publish dir.

    WHY: an export is outside the contract; written inside the publish
    directory it would become a foreign entry the next publish refuses.
    """
    real = _resolve_out_dir(target)
    if real.is_relative_to(source.resolve()):
        msg = f"--out {target} is inside the publish directory {source}"
        raise PublishRefused(msg)
    return real
