"""Tests for the reservoir AOI module."""

import pytest

from smol_frontier.aoi import (
    reservoir_area_ha,
    utm_geometry,
    wgs84_bbox,
    wgs84_geometry,
)


def test_reservoir_area_matches_reference() -> None:
    assert reservoir_area_ha() == pytest.approx(1261.75, abs=0.1)


def test_bbox_lies_around_endho() -> None:
    minx, miny, maxx, maxy = wgs84_bbox()
    assert -99.5 < minx < maxx < -99.2
    assert 20.0 < miny < maxy < 20.3


def test_geometries_valid_single_polygon() -> None:
    for geom in (wgs84_geometry(), utm_geometry()):
        assert geom.is_valid
        assert not geom.is_empty
        assert geom.geom_type in ("Polygon", "MultiPolygon")
