"""Pure functions behind the contract's derived figures (publish.aggregates)."""

import pytest

from smol_frontier.publish.aggregates import display_ha, display_status, hectare_cutoff
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
