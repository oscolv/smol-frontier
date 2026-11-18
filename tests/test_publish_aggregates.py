"""Pure functions behind the contract's derived figures (publish.aggregates)."""

import pytest

from smol_frontier.publish.aggregates import display_status

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
