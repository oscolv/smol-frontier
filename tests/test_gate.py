"""Tests for the identity gate's pure logic (offline, hand-made tables).

The live gate run is the `smolf gate identity` CLI, never a pytest test.
"""

import csv
from datetime import date
from pathlib import Path

import pytest

from smol_frontier.gate import (
    CSV_FIELDS,
    ComparedScene,
    GateTarget,
    MeasuredScene,
    ReferenceRow,
    RunMeta,
    compare,
    compared_error,
    load_reference,
    load_sample_targets,
    map_light,
    map_regime,
    render_summary,
    summarize,
    write_csv,
)


def _ref(item_id: str = "S2A_14QMH_20200101_0_L2A", **over) -> ReferenceRow:
    base = {
        "date": date(2020, 1, 1),
        "item_id": item_id,
        "dense_ha": 100.0,
        "interface_ha": 50.0,
        "total_ha": 150.0,
        "water_ha": 900.0,
        "soil_ha": 10.0,
        "valid_frac": 0.98,
        "light": "GOOD",
        "regime": "clean",
    }
    base.update(over)
    return ReferenceRow(**base)


def _measured(**over) -> MeasuredScene:
    base = {
        "item_id": "S2A_14QMH_20200101_0_L2A",
        "backend": "earth-search",
        "fallback": False,
        "note": "",
        "dense_ha": 100.0,
        "interface_ha": 50.0,
        "total_ha": 150.0,
        "water_ha": 900.0,
        "soil_ha": 10.0,
        "valid_frac": 0.98,
        "light": "GOOD",
        "regime": "",
    }
    base.update(over)
    return MeasuredScene(**base)


def _row(
    delta_total: float, ref_item: str = "S2A_14QMH_20200101_0_L2A", **over
) -> ComparedScene:
    """Compared row with ours_total = ref_total + delta (ref_total=150)."""
    ref = _ref(ref_item)
    return compare(ref, _measured(total_ha=ref.total_ha + delta_total, **over))


def test_map_light() -> None:
    assert map_light("BUENA") == "GOOD"
    assert map_light("DUDOSA") == "DOUBTFUL"
    assert map_light("MALA") == "POOR"
    with pytest.raises(ValueError, match="unknown reference light"):
        map_light("REGULAR")


def test_map_regime() -> None:
    assert map_regime("LECHO") == "EXPOSED_BED"
    assert map_regime("TURBIDEZ") == "TURBIDITY"
    assert map_regime("") == "clean"
    with pytest.raises(ValueError, match="unknown reference regime"):
        map_regime("NIEBLA")


def test_compare_deltas_are_ours_minus_ref() -> None:
    row = compare(
        _ref(),
        _measured(
            dense_ha=110.0,
            interface_ha=45.0,
            total_ha=155.0,
            water_ha=890.0,
            soil_ha=12.0,
            valid_frac=0.97,
        ),
    )
    assert row.delta_dense_ha == pytest.approx(10.0)
    assert row.delta_interface_ha == pytest.approx(-5.0)
    assert row.delta_total_ha == pytest.approx(5.0)
    assert row.abs_delta_total_ha == pytest.approx(5.0)
    assert row.delta_water_ha == pytest.approx(-10.0)
    assert row.delta_soil_ha == pytest.approx(2.0)
    assert row.delta_valid_frac == pytest.approx(-0.01)
    assert row.light_match is True
    assert row.regime_match is True


def test_compare_empty_regime_means_clean() -> None:
    row = compare(_ref(regime="clean"), _measured(regime=""))
    assert row.ours_regime == "clean"
    assert row.regime_match is True
    row = compare(_ref(regime="EXPOSED_BED"), _measured(regime="EXPOSED_BED"))
    assert row.regime_match is True


def test_compare_records_fallback() -> None:
    row = compare(
        _ref(),
        _measured(
            item_id="S2A_MSIL2A_PCENE",
            backend="planetary-computer",
            fallback=True,
            note="earth-search failed",
        ),
        stratum="control",
    )
    assert row.fallback is True
    assert row.backend_used == "planetary-computer"
    assert row.item_id_used == "S2A_MSIL2A_PCENE"
    assert row.stratum == "control"


def test_summarize_pass_with_fallback_row() -> None:
    rows = [
        _row(2.0, "scene-a"),
        _row(-4.0, "scene-b"),
        _row(10.0, "scene-c"),
        _row(50.0, "scene-pc", backend="planetary-computer", fallback=True),
    ]
    summary = summarize(rows)
    assert summary.passed is True
    assert summary.same_item.n == 3
    assert summary.same_item.median_abs == pytest.approx(4.0)
    assert summary.same_item.p95_abs == pytest.approx(9.4)
    assert summary.same_item.max_abs == pytest.approx(10.0)
    assert summary.same_item.max_abs_scene.endswith("scene-c")
    assert summary.same_item.light_match == 3
    # The fallback row is reported separately and never gates the verdict.
    assert summary.fallback.n == 1
    assert summary.fallback.median_abs == pytest.approx(50.0)
    assert summary.n_errors == 0


def test_summarize_median_over_budget_fails() -> None:
    assert summarize([_row(11.0), _row(12.0)]).passed is False


def test_summarize_p95_over_budget_fails() -> None:
    summary = summarize([_row(0.0), _row(0.0), _row(28.0)])
    assert summary.same_item.median_abs == pytest.approx(0.0)
    assert summary.same_item.p95_abs == pytest.approx(25.2)
    assert summary.passed is False


def test_summarize_boundary_passes() -> None:
    assert summarize([_row(10.0), _row(-10.0)]).passed is True


def test_summarize_single_scene() -> None:
    summary = summarize([_row(5.0)])
    assert summary.passed is True
    assert summary.same_item.median_abs == pytest.approx(5.0)
    assert summary.same_item.p95_abs == pytest.approx(5.0)


def test_summarize_error_row_fails_gate() -> None:
    rows = [_row(1.0), compared_error("2020-01-02", "missing", "", "boom")]
    summary = summarize(rows)
    assert summary.passed is False
    assert summary.n_errors == 1
    assert "boom" in summary.error_scenes[0]
    # The error row is excluded from the stats, not counted as a mismatch.
    assert summary.same_item.n == 1
    assert summary.light_mismatches == []


def test_summarize_no_same_item_fails() -> None:
    rows = [_row(1.0, backend="planetary-computer", fallback=True)]
    summary = summarize(rows)
    assert summary.passed is False
    assert summary.fail_reason == "no same-item scenes"


def test_summarize_lists_mismatches() -> None:
    rows = [
        _row(1.0, light="DOUBTFUL", regime="TURBIDITY"),
        _row(2.0),
    ]
    summary = summarize(rows)
    assert summary.passed is True  # labels are reported, not gated
    assert len(summary.light_mismatches) == 1
    assert len(summary.regime_mismatches) == 1
    assert summary.same_item.light_match == 1
    assert summary.same_item.regime_match == 1


def test_render_summary_reports_verdict_and_mismatches() -> None:
    rows = [_row(1.0, light="DOUBTFUL"), compared_error("2020-01-02", "x", "", "boom")]
    meta = RunMeta(
        timestamp="t", mode="sample", n_requested=2, workers=1, wall_seconds=61.0
    )
    text = render_summary(summarize(rows), meta)
    assert "Verdict: FAIL" in text
    assert "ref=GOOD ours=DOUBTFUL" in text
    assert "boom" in text
    assert "wall time: 00:01:01" in text


REF_HEADER = (
    "fecha,item_id,quality,regimen,valid_frac,lirio_denso_ha,"
    "interfase_ha,lirio_total_ha,agua_ha,suelo_ha\n"
)


def test_load_reference_maps_spanish_labels(tmp_path: Path) -> None:
    path = tmp_path / "ref.csv"
    path.write_text(
        REF_HEADER
        + "2020-01-01,S2A_14QMH_20200101_0_L2A,BUENA,,0.98,100,50,150,900,10\n"
        + "2020-01-06,S2B_14QMH_20200106_0_L2A,DUDOSA,LECHO,0.90,90,40,130,800,60\n",
        encoding="utf-8",
    )
    rows = load_reference(path)
    assert [row.item_id for row in rows] == [
        "S2A_14QMH_20200101_0_L2A",
        "S2B_14QMH_20200106_0_L2A",
    ]
    assert (rows[0].light, rows[0].regime) == ("GOOD", "clean")
    assert (rows[1].light, rows[1].regime) == ("DOUBTFUL", "EXPOSED_BED")


def test_load_reference_bad_row_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "ref.csv"
    path.write_text(
        REF_HEADER + "2020-01-01,bad,REGULAR,,0.9,1,1,1,1,1\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match=":2:"):
        load_reference(path)


def test_load_sample_targets_joins_and_errors(tmp_path: Path) -> None:
    ref_path = tmp_path / "ref.csv"
    ref_path.write_text(
        REF_HEADER
        + "2020-01-01,S2A_14QMH_20200101_0_L2A,BUENA,,0.98,100,50,150,900,10\n",
        encoding="utf-8",
    )
    sample_path = tmp_path / "sample.csv"
    sample_path.write_text(
        "date,item_id,stratum\n"
        "2020-01-01,S2A_14QMH_20200101_0_L2A,control\n"
        "2020-02-01,S2X_MISSING,ghost\n",
        encoding="utf-8",
    )
    targets, errors = load_sample_targets(load_reference(ref_path), sample_path)
    assert len(targets) == 1
    assert isinstance(targets[0], GateTarget)
    assert targets[0].stratum == "control"
    assert len(errors) == 1
    assert errors[0].item_id_requested == "S2X_MISSING"


def test_write_csv_schema(tmp_path: Path) -> None:
    path = tmp_path / "gate.csv"
    write_csv(path, [_row(5.0), compared_error("2020-01-02", "x", "", "boom")])
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(CSV_FIELDS)
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["delta_total_ha"] == "5.0000"
    assert rows[0]["light_match"] == "true"
    assert rows[1]["error"] == "boom"
    assert rows[1]["delta_total_ha"] == ""
