"""Tests for the styled classified GeoTIFF (colormap round-trip)."""

from pathlib import Path

import numpy as np
import pytest
import rasterio

from smol_frontier.classify.geotiff import (
    BAND_DESCRIPTION,
    CLASS_COLORMAP,
    CLASS_NAMES,
    PUBLISHED_COLORMAP,
    hex_to_rgba,
    write_classified_geotiff,
    write_published_geotiff,
)
from smol_frontier.config import settings


def test_hex_to_rgba() -> None:
    assert hex_to_rgba("#1d2e42") == (29, 46, 66, 255)
    assert hex_to_rgba("#00000000") == (0, 0, 0, 0)
    with pytest.raises(ValueError, match="rrggbb"):
        hex_to_rgba("#12345")


def test_colormap_derives_from_config_palette() -> None:
    assert set(CLASS_COLORMAP) == {
        settings.CLASS_NODATA,
        settings.CLASS_WATER,
        settings.CLASS_DENSE_HYACINTH,
        settings.CLASS_INTERFACE,
        settings.CLASS_BARE_SOIL,
    }
    assert CLASS_COLORMAP[settings.CLASS_WATER] == (29, 46, 66, 255)
    assert CLASS_COLORMAP[settings.CLASS_DENSE_HYACINTH] == (21, 201, 18, 255)
    assert CLASS_COLORMAP[settings.CLASS_INTERFACE] == (142, 216, 157, 255)
    assert CLASS_COLORMAP[settings.CLASS_BARE_SOIL] == (160, 120, 60, 255)
    assert CLASS_COLORMAP[settings.CLASS_NODATA] == (0, 0, 0, 0)


def test_write_read_colormap_back(tmp_path: Path) -> None:
    class_map = np.array(
        [
            [
                settings.CLASS_NODATA,
                settings.CLASS_WATER,
                settings.CLASS_DENSE_HYACINTH,
            ],
            [settings.CLASS_INTERFACE, settings.CLASS_BARE_SOIL, settings.CLASS_NODATA],
        ],
        dtype=np.uint8,
    )
    path = write_classified_geotiff(
        tmp_path / "classified.tif",
        class_map,
        rasterio.Affine(10.0, 0.0, 0.0, 0.0, -10.0, 0.0),
        "EPSG:32614",
    )
    with rasterio.open(path) as src:
        assert src.count == 1
        assert src.dtypes[0] == "uint8"
        assert src.nodata == settings.CLASS_NODATA
        assert (src.read(1) == class_map).all()
        back = src.colormap(1)
        for code, rgba in CLASS_COLORMAP.items():
            # RGB round-trips; the TIFF palette tag has no alpha channel.
            assert back[code][:3] == rgba[:3]
        assert src.descriptions[0] == BAND_DESCRIPTION
        tags = src.tags(1)
        for code, name in CLASS_NAMES.items():
            assert tags[f"class_{code}"] == name


def _guard_write(tmp_path: Path, array: np.ndarray) -> Path:
    return write_published_geotiff(
        tmp_path / "map.tif",
        array,
        rasterio.Affine(10.0, 0.0, 460_000.0, 0.0, -10.0, 2_250_000.0),
        "EPSG:32614",
    )


def test_raster_guard_rejects_float_array(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="uint8"):
        _guard_write(tmp_path, np.ones((3, 3), dtype=np.float32))
    assert not (tmp_path / "map.tif").exists()


def test_raster_guard_rejects_uint8_value_6(tmp_path: Path) -> None:
    array = np.ones((3, 3), dtype=np.uint8)
    array[1, 1] = 6
    with pytest.raises(ValueError, match="6"):
        _guard_write(tmp_path, array)
    assert not (tmp_path / "map.tif").exists()


def test_raster_guard_rejects_wider_int_and_multiband(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _guard_write(tmp_path, np.ones((3, 3), dtype=np.uint16))
    with pytest.raises(ValueError):
        _guard_write(tmp_path, np.ones((2, 3, 3), dtype=np.uint8))


def test_raster_guard_writes_classes_0_to_5_with_palette(tmp_path: Path) -> None:
    array = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.uint8)
    path = _guard_write(tmp_path, array)
    with rasterio.open(path) as src:
        assert src.dtypes[0] == "uint8"
        assert src.nodata == 0
        assert np.array_equal(src.read(1), array)
        colormap = src.colormap(1)
        assert colormap[settings.CLASS_BED_REMOVED] == hex_to_rgba(
            settings.COLOR_BED_REMOVED
        )
        assert src.tags(1)[f"class_{settings.CLASS_BED_REMOVED}"] == (
            "removed by exposed-bed correction"
        )
    assert PUBLISHED_COLORMAP[settings.CLASS_BED_REMOVED] not in {
        CLASS_COLORMAP[code] for code in CLASS_COLORMAP
    }


def test_classifier_colormap_has_no_class_5() -> None:
    assert settings.CLASS_BED_REMOVED not in CLASS_COLORMAP
    assert settings.CLASS_BED_REMOVED not in CLASS_NAMES
