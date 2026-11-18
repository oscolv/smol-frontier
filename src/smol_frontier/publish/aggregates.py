"""Pure, deterministic figures derived from published scene entries.

Every function here works on contract entries (already rounded to their
published decimals), so a derived figure always agrees with the figures a
consumer sees.
"""

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from smol_frontier.publish.errors import ContractInvalid

# WHY: display_status values. They mirror the deterministic quality gate of
#   the downstream site (decision 13, amended for contract 3.0.0): HIDDEN
#   scenes feed no published figure, LOWER_BOUND figures read "at least"
#   because part of the reservoir was not visible, FULL figures are complete.
#   A consumer's editorial holds and withdrawals are not reflected.
FULL = "FULL"
LOWER_BOUND = "LOWER_BOUND"
HIDDEN = "HIDDEN"


def display_status(entry: dict[str, object], frac_publishable: float) -> str:
    """FULL, LOWER_BOUND or HIDDEN for one published scene entry.

    HIDDEN when the scene is not GOOD (DOUBTFUL and POOR included) or has no
    corrected hyacinth figure; LOWER_BOUND when its visible fraction is below
    ``frac_publishable``; FULL otherwise.
    """
    if entry["quality_light"] != "GOOD" or entry["hyacinth_corrected_ha"] is None:
        return HIDDEN
    if float(entry["valid_frac"]) < frac_publishable:
        return LOWER_BOUND
    return FULL


# WHY: the parts of display_ha, in tie-break order: when two parts have the
#   same fractional remainder, the earlier one (dense) takes the unit.
_DISPLAY_PARTS = (
    ("dense", "dense_corrected_ha"),
    ("interface", "interface_corrected_ha"),
)


def _decimal(value: object) -> Decimal:
    """Exact decimal of a published number (its shortest repr, not the float).

    Published figures are already rounded, so ``str`` recovers the digits a
    consumer reads; the float's binary expansion would turn 378.525 into
    378.52499... and break half-up rounding.
    """
    return Decimal(str(value))


def display_ha(entry: dict[str, object]) -> dict[str, int] | None:
    """Integer hectares ``{total, dense, interface}`` whose parts sum to total.

    ``total`` is the published ``hyacinth_corrected_ha`` rounded half up. Each
    part starts at the floor of its published corrected figure; the units
    still missing go one each to the parts with the largest fractional
    remainder, ties to dense. None when any corrected figure is null.
    Raises ContractInvalid when the parts cannot reach the total (a run whose
    corrected parts do not add up to its corrected total).
    """
    total_ha = entry["hyacinth_corrected_ha"]
    parts_ha = [entry[field] for _, field in _DISPLAY_PARTS]
    if total_ha is None or any(part is None for part in parts_ha):
        return None
    total = int(_decimal(total_ha).to_integral_value(rounding=ROUND_HALF_UP))
    exact = [_decimal(part) for part in parts_ha]
    floors = [int(part.to_integral_value(rounding=ROUND_FLOOR)) for part in exact]
    missing = total - sum(floors)
    if not 0 <= missing <= len(floors):
        msg = (
            f"corrected parts {parts_ha} cannot be apportioned to a total of "
            f"{total} ha (hyacinth_corrected_ha {total_ha})"
        )
        raise ContractInvalid(msg)
    by_remainder = sorted(range(len(exact)), key=lambda i: (-(exact[i] - floors[i]), i))
    for index in by_remainder[:missing]:
        floors[index] += 1
    return {"total": total} | {
        name: value for (name, _), value in zip(_DISPLAY_PARTS, floors, strict=True)
    }


def hectare_cutoff(reservoir_area_ha: float, pct: float) -> float:
    """``reservoir_area_ha × pct / 100`` rounded half up to 2 decimals.

    Pass the published (2-decimal) area so the figure follows from the
    contract.
    """
    exact = _decimal(reservoir_area_ha) * _decimal(pct) / 100
    return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
