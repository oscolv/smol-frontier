"""The reservoir outline as a zipped shapefile, for the download bundle.

``outline_zip`` writes the dissolved AOI (EPSG:4326, full precision) as
``reservoir_outline.shp/.shx/.dbf/.prj/.cpg`` plus a ``README.txt`` and
returns the zip's bytes. Its one attribute, ``area_ha``, is the reservoir
area ``aoi.py`` computes, not the attribute of the committed input. The
bytes are deterministic: the same polygon and date give the same zip.
"""

import io
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import geopandas as gpd

from smol_frontier import aoi
from smol_frontier.config import settings

__all__ = ["OUTLINE_ZIP", "outline_zip"]

OUTLINE_ZIP = "reservoir_outline.zip"
_STEM = "reservoir_outline"
# WHY this order: the shapefile's own files first, then its README.
_MEMBERS = (
    f"{_STEM}.shp",
    f"{_STEM}.shx",
    f"{_STEM}.dbf",
    f"{_STEM}.prj",
    f"{_STEM}.cpg",
    "README.txt",
)
_WGS84 = "EPSG:4326"
# WHY: area_ha carries the published two decimals (as reservoir_area_ha).
_DECIMALS_AREA = 2


def _readme(area_ha: float, year: str, holder: str, licence_url: str) -> bytes:
    """The outline's README: source, area, CRS, licence."""
    lines = [
        "Endho Reservoir outline (vaso)",
        "==============================",
        "",
        "reservoir_outline.shp is the area of interest of the smolf water",
        "hyacinth series: the reservoir basin every published percentage is",
        "computed against.",
        "",
        "Source: created by the author from JRC Global Surface Water v1.4,",
        "occurrence >= 25 % (Pekel et al., 2016, Nature 540:418-422).",
        "",
        f"Area: {area_ha:.2f} ha (attribute area_ha), computed from the polygon",
        f"in {settings.UTM_CRS} (UTM zone 14N).",
        "",
        f"CRS: {_WGS84} (WGS 84, longitude/latitude), full precision.",
        "latest.json's reservoir_outline is the same polygon rounded to 6",
        "decimals (under 10 cm) with RFC 7946 ring orientation.",
        "",
        f"Licence: Copyright (c) {year} {holder}. Licensed under the Creative",
        "Commons Attribution 4.0 International licence (CC BY 4.0):",
        f"    {licence_url}",
        "Credit the source: JRC Global Surface Water (European Commission,",
        "Joint Research Centre).",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _fixed_dbf_date(dbf: bytes, day: date) -> bytes:
    """``dbf`` with its header's last-update date set to ``day``.

    WHY: GDAL stamps today's date into bytes 1-3 (YY since 1900, MM, DD),
    so the same polygon would give different bytes on different days.
    """
    stamp = bytes([day.year - 1900, day.month, day.day])
    return dbf[:1] + stamp + dbf[4:]


def outline_zip(day: date, holder: str, licence_url: str) -> bytes:
    """The zipped reservoir outline; ``day`` dates the dbf, zip and licence.

    WHY ``day`` from the caller: the bundle takes its date from the latest
    scene (no wall clock), so a publish always exports the same bytes.
    """
    area_ha = round(aoi.reservoir_area_ha(), _DECIMALS_AREA)
    frame = gpd.GeoDataFrame(
        {"area_ha": [area_ha]}, geometry=[aoi.wgs84_geometry()], crs=_WGS84
    )
    with tempfile.TemporaryDirectory() as tmp:
        frame.to_file(Path(tmp) / f"{_STEM}.shp", engine="pyogrio", encoding="UTF-8")
        files = {name: (Path(tmp) / name).read_bytes() for name in _MEMBERS[:-1]}
    files[f"{_STEM}.dbf"] = _fixed_dbf_date(files[f"{_STEM}.dbf"], day)
    files["README.txt"] = _readme(area_ha, str(day.year), holder, licence_url)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in _MEMBERS:
            info = zipfile.ZipInfo(name, (day.year, day.month, day.day, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return buffer.getvalue()
