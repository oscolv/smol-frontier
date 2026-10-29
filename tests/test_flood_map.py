"""Tests for the flood-stage map (pure functions on small arrays + golden rebuild)."""

import numpy as np
import rasterio

from smol_frontier.aoi import utm_geometry
from smol_frontier.hydro.flood_map import (
    DEFAULT_FLOOD_PATH,
    FLOOD_NODATA,
    FloodGrid,
    assign_stages,
    build_flood_map,
    fill_interior_sinks,
    pixel_ranking,
)
from smol_frontier.ingest import _inside_mask  # same rasterizer ingest uses


def test_fill_removes_interior_pocket_keeps_edge_pocket() -> None:
    mask = np.ones((7, 7), dtype=bool)
    mask[-1, :] = False
    occ = np.full((7, 7), 100.0)
    occ[3:5, 3:5] = 5.0  # interior pocket: historical mat, fills
    occ[0:2, 0:2] = 5.0  # shore-connected pocket: real bed, stays
    filled = fill_interior_sinks(occ, mask)
    assert (filled[3:5, 3:5] == 100.0).all()
    assert (filled[0:2, 0:2] == 5.0).all()
    rest = mask.copy()
    rest[3:5, 3:5] = False
    rest[0:2, 0:2] = False
    assert (filled[rest] == 100.0).all()
    assert (filled[~mask] == 0.0).all()


def test_pixel_ranking_orders_occurrence_then_distance() -> None:
    mask = np.zeros((5, 5), dtype=bool)
    mask[1:4, 1:4] = True  # False border = shore for the distance transform
    occ = np.zeros((5, 5))
    occ[1:4, 1:4] = [
        [50.0, 20.0, 30.0],
        [20.0, 50.0, 40.0],
        [60.0, 70.0, 80.0],
    ]
    # Occurrence desc; the 50-tie breaks by distance (center first);
    # the 20-tie (equal distance) breaks by raster order.
    assert pixel_ranking(occ, mask).tolist() == [18, 17, 16, 12, 6, 13, 8, 7, 11]


def test_area_under_each_stage_matches_curve_within_one_pixel() -> None:
    mask = np.ones((10, 10), dtype=bool)
    occ = np.arange(100, dtype=np.float64).reshape(10, 10)
    stage_m = [2000.0, 2001.0, 2002.0]
    area_ha = [0.2, 0.5, 1.0]  # max == mask area (100 px x 0.01 ha)
    flood = assign_stages(pixel_ranking(occ, mask), (10, 10), mask, stage_m, area_ha)
    for stage in set(flood[mask].tolist()) | {2000.0, 2001.0, 2002.0}:
        map_ha = float((flood[mask] <= stage).sum()) * 0.01
        curve_ha = float(np.interp(stage, stage_m, area_ha))
        assert abs(map_ha - curve_ha) <= 0.01


def test_assign_clips_below_and_above_curve_range() -> None:
    mask = np.ones((1, 5), dtype=bool)
    occ = np.array([[50.0, 40.0, 30.0, 20.0, 10.0]])
    ranked = pixel_ranking(occ, mask)
    flood = assign_stages(ranked, (1, 5), mask, [2000.0, 2001.0], [0.02, 0.04])
    assert flood.ravel()[ranked[0]] == 2000.0  # 0.01 ha < curve min
    assert flood.ravel()[ranked[-1]] == 2001.0  # 0.05 ha > curve max


def test_assign_marks_outside_nodata() -> None:
    mask = np.ones((3, 3), dtype=bool)
    mask[0, 0] = False
    occ = np.arange(9, dtype=np.float64).reshape(3, 3)
    flood = assign_stages(pixel_ranking(occ, mask), (3, 3), mask, [2000.0], [0.08])
    assert flood[0, 0] == FLOOD_NODATA
    assert (flood[mask] != FLOOD_NODATA).all()


def test_rebuild_reproduces_committed_flood_map(tmp_path) -> None:
    """Golden test: rebuild from the committed clip + curve matches the tif byte-for-byte.

    No network (clip reused, cache untouched: the grid comes from the
    committed tif and the mask is rasterized with ingest's function).
    """
    with rasterio.open(DEFAULT_FLOOD_PATH) as src:
        transform, shape, crs = src.transform, (src.height, src.width), src.crs
    mask = _inside_mask(utm_geometry(), crs, transform, shape)
    grid = FloodGrid(transform=transform, shape=shape, crs=str(crs), mask=mask)
    target = tmp_path / "flood_stage.tif"
    build_flood_map(grid=grid, flood_path=target)
    assert target.read_bytes() == DEFAULT_FLOOD_PATH.read_bytes()
