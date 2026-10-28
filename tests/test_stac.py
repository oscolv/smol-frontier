"""Tests for STAC scene search (HTTP mocked; no network in CI)."""

import json
import logging
import os
from datetime import date

import pystac
import pytest
import responses
from requests.exceptions import ConnectTimeout

from smol_frontier.sources.stac import (
    REQUIRED_BANDS,
    deduplicate,
    item_to_scene,
    search_scenes,
)

PC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
ES_URL = "https://earth-search.aws.element84.com/v1"

_MISSING = object()

_PC_ASSETS = {
    name: f"https://pc.example/{name}.tif"
    for name in ("B02", "B03", "B04", "B08", "B8A", "B11", "SCL")
} | {"visual": "https://pc.example/visual.tif"}

_ES_ASSETS = {
    name: f"https://es.example/{name}.tif"
    for name in ("blue", "green", "red", "nir", "nir08", "swir16", "scl")
} | {"thumbnail": "https://es.example/thumbnail.jpg"}


def _item_dict(
    item_id,
    assets,
    *,
    dt="2026-10-12T17:05:00Z",
    tile="14QMG",
    baseline="05.11",
    cloud=5.0,
    boa=_MISSING,
):
    properties = {
        "datetime": dt,
        "eo:cloud_cover": cloud,
        "s2:mgrs_tile": tile,
        "s2:processing_baseline": baseline,
    }
    if boa is not _MISSING:
        properties["earthsearch:boa_offset_applied"] = boa
    return {
        "id": item_id,
        "type": "Feature",
        "stac_version": "1.0.0",
        "collection": "sentinel-2-l2a",
        "geometry": {"type": "Point", "coordinates": [-99.37, 20.12]},
        "bbox": [-99.37, 20.12, -99.37, 20.12],
        "properties": properties,
        "links": [],
        "assets": {name: {"href": href} for name, href in assets.items()},
        "stac_extensions": [],
    }


def _landing(url):
    return {
        "id": "test",
        "type": "Catalog",
        "stac_version": "1.0.0",
        "description": "test",
        "conformsTo": [
            "https://api.stacspec.org/v1.0.0/core",
            "https://api.stacspec.org/v1.0.0/item-search",
            "https://api.stacspec.org/v1.0.0/item-search#query",
        ],
        "links": [
            {"rel": "self", "href": url, "type": "application/json"},
            {
                "rel": "search",
                "href": f"{url}/search",
                "type": "application/geo+json",
                "method": "POST",
            },
        ],
    }


def _results(items):
    return {"type": "FeatureCollection", "features": items, "links": []}


def _mock_backend(url, items):
    responses.add(responses.GET, url, json=_landing(url))
    responses.add(responses.POST, f"{url}/search", json=_results(items))


def test_band_normalization_planetary_computer() -> None:
    item = pystac.Item.from_dict(_item_dict("pc-item", _PC_ASSETS))
    scene = item_to_scene(item, source="planetary-computer")
    assert scene is not None
    assert set(scene.assets) == set(REQUIRED_BANDS)
    assert scene.assets["B02"] == "https://pc.example/B02.tif"
    assert scene.assets["SCL"] == "https://pc.example/SCL.tif"


def test_band_normalization_earth_search() -> None:
    item = pystac.Item.from_dict(_item_dict("es-item", _ES_ASSETS, boa=True))
    scene = item_to_scene(item, source="earth-search")
    assert scene is not None
    assert set(scene.assets) == set(REQUIRED_BANDS)
    assert scene.assets["B02"] == "https://es.example/blue.tif"
    assert scene.assets["B8A"] == "https://es.example/nir08.tif"
    assert scene.assets["B11"] == "https://es.example/swir16.tif"


def test_boa_offset_flag() -> None:
    pc_scene = item_to_scene(
        pystac.Item.from_dict(_item_dict("pc-item", _PC_ASSETS)),
        source="planetary-computer",
    )
    assert pc_scene is not None
    assert pc_scene.boa_offset_applied is False
    es_scene = item_to_scene(
        pystac.Item.from_dict(_item_dict("es-item", _ES_ASSETS, boa=True)),
        source="earth-search",
    )
    assert es_scene is not None
    assert es_scene.boa_offset_applied is True


def test_dedup_keeps_highest_baseline() -> None:
    old = pystac.Item.from_dict(_item_dict("old", _PC_ASSETS, baseline="04.00"))
    new = pystac.Item.from_dict(_item_dict("new", _PC_ASSETS, baseline="05.11"))
    other_tile = pystac.Item.from_dict(
        _item_dict("other", _PC_ASSETS, tile="14QMH", baseline="04.00")
    )
    result = deduplicate([old, new, other_tile])
    assert [item.id for item in result] == ["new", "other"]


def test_dedup_missing_baseline_loses() -> None:
    missing = pystac.Item.from_dict(_item_dict("missing", _PC_ASSETS, baseline=None))
    assert deduplicate([missing]) == [missing]
    known = pystac.Item.from_dict(_item_dict("known", _PC_ASSETS, baseline="04.00"))
    assert [item.id for item in deduplicate([missing, known])] == ["known"]


@responses.activate
def test_search_sends_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("smol_frontier.sources.stac._sign_item", lambda item: item)
    _mock_backend(PC_URL, [_item_dict("pc-item", _PC_ASSETS)])
    bbox = (-99.4, 20.0, -99.3, 20.2)
    scenes = search_scenes(date(2026, 10, 12), date(2026, 10, 12), 20.0, bbox=bbox)
    assert [call.request.method for call in responses.calls].count("POST") == 1
    post = next(call for call in responses.calls if call.request.method == "POST")
    assert post.request.url == f"{PC_URL}/search"
    body = json.loads(post.request.body)
    assert body["collections"] == ["sentinel-2-l2a"]
    assert body["bbox"] == list(bbox)
    assert body["datetime"] == "2026-10-12T00:00:00Z/2026-10-12T23:59:59Z"
    assert body["query"] == {"eo:cloud_cover": {"lt": 20.0}}
    assert len(scenes) == 1
    (scene,) = scenes
    assert scene.id == "pc-item"
    assert scene.source == "planetary-computer"
    assert scene.processing_baseline == "05.11"
    assert scene.boa_offset_applied is False
    assert set(scene.assets) == set(REQUIRED_BANDS)


@responses.activate
def test_fallback_on_timeout(caplog: pytest.LogCaptureFixture) -> None:
    responses.add(responses.GET, PC_URL, json=_landing(PC_URL))
    responses.add(responses.POST, f"{PC_URL}/search", body=ConnectTimeout())
    _mock_backend(ES_URL, [_item_dict("es-item", _ES_ASSETS, boa=True)])
    with caplog.at_level(logging.INFO, logger="smol_frontier.sources.stac"):
        scenes = search_scenes(date(2026, 10, 12), date(2026, 10, 12), 20.0)
    assert len(scenes) == 1
    assert scenes[0].source == "earth-search"
    assert scenes[0].boa_offset_applied is True
    assert "served by earth-search" in caplog.text


@responses.activate
def test_fallback_on_http_error(caplog: pytest.LogCaptureFixture) -> None:
    responses.add(responses.GET, PC_URL, json=_landing(PC_URL))
    responses.add(responses.POST, f"{PC_URL}/search", status=500)
    _mock_backend(ES_URL, [_item_dict("es-item", _ES_ASSETS, boa=True)])
    with caplog.at_level(logging.INFO, logger="smol_frontier.sources.stac"):
        scenes = search_scenes(date(2026, 10, 12), date(2026, 10, 12), 20.0)
    assert len(scenes) == 1
    assert scenes[0].source == "earth-search"
    assert "served by earth-search" in caplog.text


@responses.activate
def test_all_backends_fail() -> None:
    for url in (PC_URL, ES_URL):
        responses.add(responses.GET, url, json=_landing(url))
        responses.add(responses.POST, f"{url}/search", status=500)
    with pytest.raises(RuntimeError, match="all STAC backends failed"):
        search_scenes(date(2026, 10, 12), date(2026, 10, 12), 20.0)


@pytest.mark.skipif(
    os.environ.get("SMOLF_LIVE") != "1",
    reason="opt-in live STAC test (SMOLF_LIVE=1)",
)
def test_live_search_2026_10_12() -> None:
    scenes = search_scenes(date(2026, 10, 12), date(2026, 10, 12), 100.0)
    print(f"\nSMOLF_LIVE: {len(scenes)} scene(s) for 2026-10-12")
    for scene in scenes:
        print(
            f"  {scene.id} | {scene.source} | baseline={scene.processing_baseline}"
            f" | boa={scene.boa_offset_applied}"
        )
    for scene in scenes:
        assert set(scene.assets) == set(REQUIRED_BANDS)
        assert all(href.startswith("https://") for href in scene.assets.values())
