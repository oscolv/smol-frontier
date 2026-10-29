"""Tests for the frozen settings object."""

import dataclasses
from pathlib import Path

import pytest

from smol_frontier.config import PROJECT_ROOT, Settings, settings


def test_settings_are_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.TAU_WATER = 0.5  # type: ignore[misc]


def test_aoi_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMOLF_AOI", raising=False)
    assert Settings().AOI_PATH == PROJECT_ROOT / "data/aoi/endho_vaso.shp"
    assert Settings().AOI_PATH.is_absolute()


def test_aoi_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMOLF_AOI", "elsewhere/custom.shp")
    assert Settings().AOI_PATH == Path("elsewhere/custom.shp")


def test_class_codes_are_unique() -> None:
    codes = [
        settings.CLASS_NODATA,
        settings.CLASS_WATER,
        settings.CLASS_DENSE_HYACINTH,
        settings.CLASS_INTERFACE,
        settings.CLASS_BARE_SOIL,
    ]
    assert codes == [0, 1, 2, 3, 4]
    assert len(set(codes)) == len(codes)


def test_thresholds_in_unit_interval() -> None:
    values = (
        settings.TAU_WATER,
        settings.TAU_HYACINTH,
        settings.FRAC_PUBLISHABLE,
        settings.MIN_VALID_FRACTION,
        settings.CLOUD_FRAC_DOUBTFUL,
        settings.FRAC_DOUBTFUL,
        settings.REGIME_NIR_WATER_MIN,
        settings.TURBIDITY_HIGH_FRAC,
    )
    for value in values:
        assert 0.0 < value < 1.0


def test_quality_constants() -> None:
    assert settings.MIN_VALID_FRACTION == 0.80
    assert settings.CLOUD_FRAC_DOUBTFUL == 0.10
    assert settings.FRAC_DOUBTFUL == 0.85
    assert settings.SEP_DOUBTFUL == 1.5
    assert settings.REGIME_SOIL_MAX_HA == 50.0
    assert settings.REGIME_NIR_WATER_MIN == 0.60
    assert settings.TURBIDITY_HIGH_FRAC == 0.70


def test_hydro_constants() -> None:
    assert settings.LEVEL_REVISION_TOL_M == 0.05


def test_bed_constants() -> None:
    assert settings.BED_CUSHION_MODEL_HA == (
        (2005.0, 29.0),
        (2010.0, 41.0),
        (2015.0, 25.0),
        (2018.0, 66.0),
    )


def test_curve_constants() -> None:
    assert settings.CURVE_DATA_END.isoformat() == "2026-09-20"
    assert settings.CURVE_BIN_M == 0.10
    assert settings.CURVE_BIN_MIN_N == 5
    assert settings.CURVE_FIT_H_MAX == 2017.5
    assert settings.CURVE_ANCHORS == ((2003.16, 52.1), (2018.0, 182.0), (2020.0, 208.0))
    assert settings.CURVE_W_ANCHOR == 50.0
    assert settings.CURVE_BOOTSTRAP_B == 300
    assert settings.CURVE_BOOTSTRAP_SEED == 42
    assert settings.CURVE_GRID_TOP == 2018.5


def test_stage1_constants() -> None:
    assert settings.BORDER_ZONE_M == 150.0
    assert settings.SOIL_BSI_MIN == 0.02
    assert settings.SOIL_MNDWI_MAX == -0.12
    assert settings.SOIL_NDVI_MAX == 0.25
    assert settings.SOIL_NDRE_MAX == 0.25
    assert settings.SOIL_NSD_MAX == 0.15
    assert settings.NIR_WATER_MAX == 0.12
    assert settings.NSD_VEG_MIN == 0.15
    assert settings.FAI_FLOAT_MIN == 0.04


def test_traffic_light_bands() -> None:
    assert settings.traffic_light(0.0) == "green"
    assert settings.traffic_light(29.9) == "green"
    assert settings.traffic_light(30.0) == "yellow"
    assert settings.traffic_light(49.9) == "yellow"
    assert settings.traffic_light(50.0) == "red"
    assert settings.traffic_light(100.0) == "red"
