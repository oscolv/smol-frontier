"""Tests for the styled classified GeoTIFF (colormap round-trip)."""

from pathlib import Path

import numpy as np
import pytest
import rasterio

from smol_frontier.classify.geotiff import (
    BAND_DESCRIPTION,
    CLASS_COLORMAP,
    CLASS_NAMES,
    hex_to_rgba,
    write_classified_geotiff,
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
