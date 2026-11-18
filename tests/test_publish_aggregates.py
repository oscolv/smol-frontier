"""Pure functions behind the contract's derived figures (publish.aggregates)."""

import pytest

from smol_frontier.publish.aggregates import (
    display_ha,
    display_status,
    hectare_cutoff,
    lower_median,
    monthly_medians,
    same_month,
)
from smol_frontier.publish.errors import ContractInvalid

FRAC = 0.95


def _entry(**overrides: object) -> dict:
    entry = {
        "quality_light": "GOOD",
        "valid_frac": 0.99,
        "hyacinth_corrected_ha": 630.56,
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({}, "FULL"),
        ({"valid_frac": FRAC}, "FULL"),
        ({"valid_frac": 0.9499}, "LOWER_BOUND"),
        ({"valid_frac": 0.81}, "LOWER_BOUND"),
        ({"quality_light": "DOUBTFUL"}, "HIDDEN"),
        ({"quality_light": "DOUBTFUL", "valid_frac": 1.0}, "HIDDEN"),
        ({"quality_light": "POOR", "hyacinth_corrected_ha": None}, "HIDDEN"),
        ({"hyacinth_corrected_ha": None}, "HIDDEN"),
        ({"hyacinth_corrected_ha": None, "valid_frac": 0.9}, "HIDDEN"),
        ({"hyacinth_corrected_ha": 0.0}, "FULL"),
    ],
)
def test_display_status(overrides: dict, status: str) -> None:
    assert display_status(_entry(**overrides), FRAC) == status


def _corrected(total: float | None, dense: float | None, interface: float | None):
    return {
        "hyacinth_corrected_ha": total,
        "dense_corrected_ha": dense,
        "interface_corrected_ha": interface,
    }


@pytest.mark.parametrize(
    ("figures", "expected"),
    [
        ((630.56, 399.88, 230.68), (631, 400, 231)),
        ((818.34, 350.24, 468.10), (818, 350, 468)),
        ((10.0, 4.3, 5.7), (10, 4, 6)),
        ((10.0, 4.7, 5.3), (10, 5, 5)),
        ((10.0, 4.5, 5.5), (10, 5, 5)),
        ((1.0, 0.5, 0.5), (1, 1, 0)),
        ((10.5, 5.25, 5.25), (11, 6, 5)),
        ((2.5, 1.25, 1.25), (3, 2, 1)),
        ((0.5, 0.3, 0.2), (1, 1, 0)),
        ((0.49, 0.25, 0.24), (0, 0, 0)),
        ((10.49, 5.25, 5.25), (10, 5, 5)),
        ((0.0, 0.0, 0.0), (0, 0, 0)),
    ],
)
def test_display_ha_sums_to_rounded_total(
    figures: tuple[float, float, float], expected: tuple[int, int, int]
) -> None:
    result = display_ha(_corrected(*figures))
    assert result == dict(zip(("total", "dense", "interface"), expected, strict=True))
    assert all(type(value) is int for value in result.values())


@pytest.mark.parametrize(
    "figures",
    [(None, None, None), (None, 1.0, 1.0), (2.0, None, 1.0), (2.0, 1.0, None)],
)
def test_display_ha_null_when_corrected_figures_are_null(figures: tuple) -> None:
    assert display_ha(_corrected(*figures)) is None


@pytest.mark.parametrize("figures", [(20.0, 5.0, 5.0), (3.0, 5.0, 5.0)])
def test_display_ha_refuses_parts_that_cannot_reach_the_total(
    figures: tuple[float, float, float],
) -> None:
    with pytest.raises(ContractInvalid):
        display_ha(_corrected(*figures))


@pytest.mark.parametrize(
    ("area", "pct", "expected"),
    [
        (1261.75, 30.0, 378.53),
        (1261.75, 50.0, 630.88),
        (1000.0, 30.0, 300.0),
    ],
)
def test_hectare_cutoff(area: float, pct: float, expected: float) -> None:
    assert hectare_cutoff(area, pct) == expected


def _scene(day: str, ha: float | None, status: str = "FULL") -> dict:
    return {"date": day, "hyacinth_corrected_ha": ha, "display_status": status}


def _median_of(scenes: list[dict]) -> tuple[float | None, int]:
    (entry,) = monthly_medians(scenes)
    return entry["median_ha"], entry["n_scenes"]


def test_monthly_median_odd_count_is_middle_value() -> None:
    scenes = [
        _scene("2024-03-01", 30.0),
        _scene("2024-03-11", 10.0),
        _scene("2024-03-21", 20.0, "LOWER_BOUND"),
    ]
    assert _median_of(scenes) == (20.0, 3)


def test_monthly_median_even_count_is_mean_of_middle_pair() -> None:
    scenes = [
        _scene("2024-03-01", 10.0),
        _scene("2024-03-06", 40.0),
        _scene("2024-03-11", 20.0),
        _scene("2024-03-16", 31.0),
    ]
    assert _median_of(scenes) == (25.5, 4)


def test_monthly_median_even_count_rounds_half_up_on_exact_decimals() -> None:
    scenes = [_scene("2024-03-01", 378.52), _scene("2024-03-06", 378.53)]
    assert _median_of(scenes) == (378.53, 2)


def test_monthly_median_single_scene() -> None:
    assert _median_of([_scene("2024-03-01", 818.34)]) == (818.34, 1)


def test_monthly_median_all_hidden_month_is_null_and_zero() -> None:
    scenes = [
        _scene("2024-03-01", None, "HIDDEN"),
        _scene("2024-03-06", 500.0, "HIDDEN"),
    ]
    assert _median_of(scenes) == (None, 0)


def test_monthly_median_ignores_hidden_scenes_in_a_mixed_month() -> None:
    scenes = [
        _scene("2024-03-01", 10.0),
        _scene("2024-03-06", 999.0, "HIDDEN"),
        _scene("2024-03-11", 20.0, "LOWER_BOUND"),
    ]
    assert _median_of(scenes) == (15.0, 2)


def test_monthly_medians_span_is_contiguous_across_years() -> None:
    scenes = [
        _scene("2023-11-15", None, "HIDDEN"),
        _scene("2024-02-03", 12.0),
    ]
    assert monthly_medians(scenes) == [
        {"month": "2023-11", "median_ha": None, "n_scenes": 0},
        {"month": "2023-12", "median_ha": None, "n_scenes": 0},
        {"month": "2024-01", "median_ha": None, "n_scenes": 0},
        {"month": "2024-02", "median_ha": 12.0, "n_scenes": 1},
    ]


def test_monthly_medians_order_does_not_depend_on_input_order() -> None:
    scenes = [_scene("2024-02-03", 12.0), _scene("2024-01-10", 4.0)]
    assert [e["month"] for e in monthly_medians(scenes)] == ["2024-01", "2024-02"]


def test_monthly_medians_empty() -> None:
    assert monthly_medians([]) == []


def _pick(day: str, ha: float, item_id: str = "X", status: str = "FULL") -> dict:
    return {
        "date": day,
        "item_id": item_id,
        "hyacinth_corrected_ha": ha,
        "display_status": status,
    }


def test_lower_median_odd_count_is_middle_scene() -> None:
    scenes = [_pick("2024-01-01", 30.0), _pick("2024-01-02", 10.0)]
    scenes.append(_pick("2024-01-03", 20.0))
    assert lower_median(scenes)["date"] == "2024-01-03"


def test_lower_median_even_count_is_lower_of_middle_pair() -> None:
    scenes = [
        _pick("2024-01-01", 40.0),
        _pick("2024-01-02", 10.0),
        _pick("2024-01-03", 30.0),
        _pick("2024-01-04", 20.0),
    ]
    assert lower_median(scenes)["hyacinth_corrected_ha"] == 20.0


def test_lower_median_ties_broken_by_date_then_item_id() -> None:
    scenes = [
        _pick("2024-01-09", 10.0, "A"),
        _pick("2024-01-05", 10.0, "Z"),
        _pick("2024-01-05", 10.0, "B"),
    ]
    assert [lower_median(scenes)[k] for k in ("date", "item_id")] == [
        "2024-01-05",
        "Z",
    ]
    pair = [_pick("2024-01-05", 10.0, "Z"), _pick("2024-01-05", 10.0, "B")]
    assert lower_median(pair)["item_id"] == "B"


def test_lower_median_compares_exact_decimals_not_text() -> None:
    scenes = [_pick("2024-01-01", 9.5), _pick("2024-01-02", 10.25)]
    assert lower_median(scenes)["hyacinth_corrected_ha"] == 9.5


def test_lower_median_empty_is_none() -> None:
    assert lower_median([]) is None


def test_same_month_none_without_full_scene() -> None:
    scenes = [
        _pick("2024-01-01", 10.0, status="LOWER_BOUND"),
        _pick("2024-02-01", 10.0, status="HIDDEN"),
    ]
    assert same_month(scenes) is None
    assert same_month([]) is None


def test_same_month_month_of_latest_full_years_from_first_scene() -> None:
    scenes = [
        _pick("2022-11-30", 1.0, status="HIDDEN"),
        _pick("2023-03-02", 7.0, "A"),
        _pick("2023-03-20", 5.0, "B", "LOWER_BOUND"),
        _pick("2023-03-25", 1.0, "C", "HIDDEN"),
        _pick("2025-03-08", 9.0, "D"),
        _pick("2025-04-01", 2.0, "E", "LOWER_BOUND"),
        _pick("2025-05-01", 3.0, "F", "HIDDEN"),
    ]
    block = same_month(scenes)
    assert block["month"] == 3
    assert [(y["year"], y["n_scenes"]) for y in block["years"]] == [
        (2022, 0),
        (2023, 2),
        (2024, 0),
        (2025, 1),
    ]
    assert block["years"][0]["scene"] is None
    assert block["years"][1]["scene"] == _pick("2023-03-20", 5.0, "B", "LOWER_BOUND")
    assert block["years"][3]["scene"]["item_id"] == "D"


def test_same_month_scene_carries_only_the_label_fields() -> None:
    scene = _pick("2024-06-01", 4.0) | {"valid_frac": 0.99, "display_ha": None}
    [year] = same_month([scene])["years"]
    assert set(year["scene"]) == {
        "item_id",
        "date",
        "hyacinth_corrected_ha",
        "display_status",
    }
