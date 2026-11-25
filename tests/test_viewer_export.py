"""`smolf export viewer`: one folder per scene for endho's per-date viewer.

The scene rebuild and the photo read are faked at their boundaries
(``build_map``, ``build_true_colour``), so these tests need no scene cache
and no network. The fixtures are the map export's (same publish and run).
"""

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest
import rasterio
from test_map_export import (  # noqa: F401  (data_dir is a fixture)
    CRS,
    RUN_ID,
    TRANSFORM,
    _fake_class_map,
    _records,
    data_dir,
    publish_run,
)
from typer.testing import CliRunner

from smol_frontier import paths
from smol_frontier.biomass import wet_biomass
from smol_frontier.classify.display import (
    decode_display_classes,
    render_display_image,
)
from smol_frontier.cli import app
from smol_frontier.publish import viewer_export
from smol_frontier.publish.errors import PublishRefused
from smol_frontier.publish.truecolour import TrueColour

FOLDERS = [
    "2024-01-01_S2A_14QMH_20240101_0_L2A",
    "2024-01-03_S2A_14QMH_20240103_0_L2A",
    "2024-01-05_S2A_14QMH_20240105_0_L2A",
]


class _Layer:
    def __init__(self, class_map: np.ndarray) -> None:
        self.class_map = class_map
        self.transform = TRANSFORM
        self.crs = CRS
        self.display = render_display_image(class_map, TRANSFORM, CRS)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Fake the scene rebuild and the photo read; record what each was asked."""
    calls: dict[str, list[str]] = {"map": [], "photo": []}

    def build(record: dict, manifest: dict, committed: object) -> _Layer:
        calls["map"].append(record["item_id_used"])
        return _Layer(_fake_class_map(record["row"]))

    def photo(record: dict, display: object) -> TrueColour:
        calls["photo"].append(record["item_id_used"])
        shape = display.classes.shape
        return TrueColour(np.full((3, *shape), 0.05), "sentinel-2-l2a")

    monkeypatch.setattr(viewer_export, "build_map", build)
    monkeypatch.setattr(viewer_export, "build_true_colour", photo)
    return calls


def _export(*args: str):
    return CliRunner().invoke(app, ["export", "viewer", *args])


def _out(data_dir: Path) -> Path:  # noqa: F811
    return data_dir / "outputs/viewer"


def _tree(out: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(out)): p.read_bytes()
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }


def _series(data_dir: Path) -> dict:  # noqa: F811
    path = data_dir / "outputs/publish/series.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_export_writes_one_folder_per_scene_with_figures(
    data_dir: Path,  # noqa: F811
    fakes: dict,
) -> None:
    result = _export()
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"exported: {paths.viewer_dir()}"
    out = _out(data_dir)
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [*FOLDERS, "index.json", "README.txt", "SHA256SUMS"]
    )
    for folder in FOLDERS:
        assert sorted(p.name for p in (out / folder).iterdir()) == [
            "classified.webp",
            "figures.json",
            "photo.webp",
        ]
    assert "S2B_14QMH_20240102_0_L2A" not in fakes["map"]
    assert len(result.stderr.splitlines()) == len(FOLDERS)


def test_figures_repeat_series_json_and_add_classes_and_biomass(
    data_dir: Path,  # noqa: F811
    fakes: dict,
) -> None:
    assert _export().exit_code == 0
    scenes = {s["item_id"]: s for s in _series(data_dir)["scenes"]}
    for folder in FOLDERS:
        figures = json.loads((_out(data_dir) / folder / "figures.json").read_text())
        scene = scenes[figures["item_id"]]
        for field in viewer_export.COPIED_FIELDS:
            assert figures[field] == scene[field], field
        assert figures["run_id"] == RUN_ID
        assert figures["attribution"] == f"Copernicus Sentinel data {scene['date'][:4]}"
        classes = figures["classes"]
        total = sum(c["ha"] for c in classes.values())
        assert round(total, 2) == figures["reservoir_ha"] == 1261.75
        mass = wet_biomass(scene["hyacinth_corrected_ha"])
        if scene["bed_applied"]:
            assert classes["dense_hyacinth"]["ha"] == scene["dense_corrected_ha"]
            assert classes["bed_removed"]["ha"] == scene["removed_ha"]
            assert figures["biomass_wet_t"] == mass.t
            assert figures["biomass_wet_t_low"] == mass.t_low
            assert figures["biomass_wet_t_high"] == mass.t_high
        else:
            assert figures["biomass_wet_t"] is None
        assert figures["biomass_density_kg_m2"] == {
            "central": 18.32,
            "low": 11.67,
            "high": 23.84,
        }


def test_classified_webp_decodes_to_the_published_classes(
    data_dir: Path,  # noqa: F811
    fakes: dict,
) -> None:
    assert _export().exit_code == 0
    path = _out(data_dir) / FOLDERS[0] / "classified.webp"
    with rasterio.open(path) as src:
        assert src.driver == "WEBP"
        classes = decode_display_classes(src.read())
    counts = np.bincount(classes.ravel(), minlength=6)
    assert counts[5] > 0 and counts[2] > 0


def test_photo_is_an_rgba_webp(data_dir: Path, fakes: dict) -> None:  # noqa: F811
    assert _export().exit_code == 0
    with rasterio.open(_out(data_dir) / FOLDERS[0] / "photo.webp") as src:
        assert src.driver == "WEBP"
        assert src.count == 4


def test_index_and_sha256sums_verify_every_file(
    data_dir: Path,  # noqa: F811
    fakes: dict,
) -> None:
    assert _export().exit_code == 0
    out = _out(data_dir)
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    series_doc = _series(data_dir)
    assert index["run_id"] == RUN_ID
    assert index["series_sha256"] == series_doc["series_sha256"]
    assert [e["folder"] for e in index["scenes"]] == FOLDERS
    for entry in index["scenes"]:
        for name, digest in entry["files"].items():
            data = (out / entry["folder"] / name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest
    lines = (out / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert len(lines) == 3 * len(FOLDERS) + 2
    for line in lines:
        digest, name = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line).groups()
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


def test_second_run_rebuilds_nothing_and_is_byte_identical(
    data_dir: Path,  # noqa: F811
    fakes: dict,
) -> None:
    assert _export().exit_code == 0
    first = _tree(_out(data_dir))
    fakes["map"].clear()
    result = _export()
    assert result.exit_code == 0
    assert fakes["map"] == []
    assert all(line.startswith("kept ") for line in result.stderr.splitlines())
    assert _tree(_out(data_dir)) == first


def test_only_a_missing_or_tampered_folder_is_rebuilt(
    data_dir: Path,  # noqa: F811
    fakes: dict,
) -> None:
    assert _export().exit_code == 0
    out = _out(data_dir)
    for name in ("photo.webp", "classified.webp", "figures.json"):
        (out / FOLDERS[0] / name).unlink()
    (out / FOLDERS[0]).rmdir()
    (out / FOLDERS[1] / "photo.webp").write_bytes(b"tampered")
    fakes["map"].clear()
    assert _export().exit_code == 0
    assert fakes["map"] == [f.split("_", 1)[1] for f in FOLDERS[:2]]


NEW_RUN = "20240111T000000"


def _second_run(tmp_path: Path, records: list[dict]) -> None:
    publish_run(tmp_path, NEW_RUN, records)


def test_a_new_run_with_the_same_figures_keeps_every_folder(
    data_dir: Path,  # noqa: F811
    fakes: dict,
    tmp_path: Path,
) -> None:
    assert _export().exit_code == 0
    first = _tree(_out(data_dir))
    _second_run(tmp_path, _records())
    fakes["map"].clear()
    fakes["photo"].clear()
    result = _export()
    assert result.exit_code == 0, result.output
    assert fakes == {"map": [], "photo": []}
    assert result.stderr.splitlines() == [f"kept {f}" for f in FOLDERS]
    out = _out(data_dir)
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert index["run_id"] == NEW_RUN
    assert index["series_sha256"] == _series(data_dir)["series_sha256"]
    assert NEW_RUN in (out / "README.txt").read_text(encoding="ascii")
    for entry in index["scenes"]:
        folder = out / entry["folder"]
        figures = json.loads((folder / "figures.json").read_text(encoding="utf-8"))
        assert figures["run_id"] == NEW_RUN
        for name in ("photo.webp", "classified.webp"):
            assert (folder / name).read_bytes() == first[f"{entry['folder']}/{name}"]
        for name, digest in entry["files"].items():
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
    for line in (out / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


def test_a_new_run_rebuilds_only_the_changed_scenes(
    data_dir: Path,  # noqa: F811
    fakes: dict,
    tmp_path: Path,
) -> None:
    assert _export().exit_code == 0
    records = _records()
    # a new cache file (map key) for one scene, a new valid_frac (figures)
    # for another, and the third scene dropped from the run
    records[0]["cache_sha256"] = "0" * 64
    records[2]["row"]["valid_frac"] = 0.97
    records[2]["valid_frac"] = 0.97
    dropped = records.pop(3)
    _second_run(tmp_path, records)
    fakes["map"].clear()
    fakes["photo"].clear()
    result = _export()
    assert result.exit_code == 0, result.output
    assert fakes["map"] == [FOLDERS[0].split("_", 1)[1], FOLDERS[1].split("_", 1)[1]]
    assert fakes["photo"] == []
    assert result.stderr.splitlines() == [
        f"built {FOLDERS[0]} (photo kept)",
        f"built {FOLDERS[1]} (photo kept)",
    ]
    out = _out(data_dir)
    assert not (out / f"{dropped['date']}_{dropped['item_id_used']}").exists()
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert [e["folder"] for e in index["scenes"]] == FOLDERS[:2]


def test_a_new_backend_reads_the_photo_again(
    data_dir: Path,  # noqa: F811
    fakes: dict,
    tmp_path: Path,
) -> None:
    assert _export().exit_code == 0
    records = _records()
    records[0]["backend_used"] = "planetary-computer"
    _second_run(tmp_path, records)
    fakes["map"].clear()
    fakes["photo"].clear()
    result = _export()
    assert result.exit_code == 0, result.output
    item_id = FOLDERS[0].split("_", 1)[1]
    assert fakes == {"map": [item_id], "photo": [item_id]}
    assert sorted(result.stderr.splitlines()) == [
        f"built {FOLDERS[0]}",
        *(f"kept {f}" for f in FOLDERS[1:]),
    ]


def test_another_smolf_version_rebuilds_every_folder(
    data_dir: Path,  # noqa: F811
    fakes: dict,
) -> None:
    assert _export().exit_code == 0
    path = _out(data_dir) / "index.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    index["smolf_version"] = "0.0.0"
    path.write_text(json.dumps(index), encoding="utf-8")
    fakes["map"].clear()
    fakes["photo"].clear()
    assert _export().exit_code == 0
    assert len(fakes["map"]) == len(FOLDERS)
    assert len(fakes["photo"]) == len(FOLDERS)


def test_photo_read_is_retried(
    data_dir: Path,  # noqa: F811
    fakes: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures = iter([True, False] * len(FOLDERS))
    real = viewer_export.build_true_colour

    def flaky(record: dict, display: object) -> TrueColour:
        if next(failures):
            raise PublishRefused("transient")
        return real(record, display)

    monkeypatch.setattr(viewer_export, "build_true_colour", flaky)
    assert _export().exit_code == 0


def test_refusal_exits_2_and_keeps_the_previous_export(
    data_dir: Path,  # noqa: F811
    fakes: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _export().exit_code == 0
    before = _tree(_out(data_dir))
    (_out(data_dir) / "index.json").unlink()  # force a rebuild of every scene
    before.pop("index.json")

    def disagree(record: dict, manifest: dict, committed: object) -> _Layer:
        class_map = _fake_class_map(record["row"])
        class_map[class_map == 5] = 1
        return _Layer(class_map)

    monkeypatch.setattr(viewer_export, "build_map", disagree)
    result = _export()
    assert result.exit_code == 2
    assert "differs from series.json" in result.stderr
    assert _tree(_out(data_dir)) == before


@pytest.mark.parametrize(
    "foreign",
    ["notes.txt", "2024-01-01_S2A_14QMH_20240101_0_L2A/notes.txt", "sub/x.webp"],
)
def test_export_refuses_out_holding_a_foreign_file(
    data_dir: Path,  # noqa: F811
    fakes: dict,
    tmp_path: Path,
    foreign: str,
) -> None:
    out = tmp_path / "viewer"
    (out / foreign).parent.mkdir(parents=True, exist_ok=True)
    (out / foreign).write_text("keep me", encoding="utf-8")
    result = _export("--out", str(out))
    assert result.exit_code == 2
    assert "does not own" in result.stderr
    assert (out / foreign).read_text(encoding="utf-8") == "keep me"


def _scene(**overrides: object) -> dict:
    scene = {
        "date": "2026-10-12",
        "item_id": "S2B_14QMH_20261012_0_L2A",
        "hyacinth_corrected_ha": 824.63,
        "dense_corrected_ha": 350.24,
        "interface_corrected_ha": 474.39,
        "display_ha": {"dense": 350, "interface": 475, "total": 825},
        "pct_reservoir_corrected": 65.36,
        "cover_band": "RED",
        "display_status": "FULL",
        "removed_ha": 0.0,
        "level_m": 2018.29,
        "valid_frac": 0.9782,
        "cloud_frac": 0.0218,
        "bed_applied": True,
        "quality_light": "GOOD",
        "regime": "CLEAN",
    }
    scene.update(overrides)
    return scene


# The 2026-10-12 control scene's class hectares (published map, run 20261119).
CONTROL_HA = {1: 415.86, 2: 350.24, 3: 474.39, 4: 0.11, 5: 0.0}


def test_control_scene_figures() -> None:
    figures = viewer_export.scene_figures(_scene(), CONTROL_HA, [[0, 0], [1, 1]], "r")
    assert figures["biomass_wet_t"] == 151072
    assert (figures["biomass_wet_t_low"], figures["biomass_wet_t_high"]) == (
        96234,
        196592,
    )
    assert figures["classes"]["no_data"] == {"ha": 21.15, "pct_reservoir": 1.68}
    assert figures["classes"]["dense_hyacinth"]["pct_reservoir"] == 27.76


def test_lower_bound_scene_has_hectares_only() -> None:
    figures = viewer_export.scene_figures(
        _scene(display_status="LOWER_BOUND"), CONTROL_HA, [[0, 0], [1, 1]], "r"
    )
    assert figures["biomass_wet_t"] is None
    assert figures["biomass_wet_t_low"] is None
    assert all(c["pct_reservoir"] is None for c in figures["classes"].values())
    assert figures["classes"]["water"]["ha"] == 415.86
    # series.json fields stay verbatim; endho decides what to show
    assert figures["pct_reservoir_corrected"] == 65.36


def test_classes_over_the_reservoir_area_are_refused() -> None:
    hectares = {**CONTROL_HA, 1: 437.10}  # 0.09 ha over the 1261.75 ha polygon
    with pytest.raises(PublishRefused, match="more than the"):
        viewer_export.scene_figures(_scene(), hectares, [[0, 0], [1, 1]], "r")


def test_owns_the_layout_only() -> None:
    owns = viewer_export.owns
    assert owns("index.json") and owns("README.txt") and owns("SHA256SUMS")
    assert owns(FOLDERS[0]) and owns(f"{FOLDERS[0]}/figures.json")
    assert not owns(f"{FOLDERS[0]}/notes.txt")
    assert not owns("index.csv") and not owns("sub")
