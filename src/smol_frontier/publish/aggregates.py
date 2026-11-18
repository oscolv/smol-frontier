"""Pure, deterministic figures derived from published scene entries.

Every function here works on contract entries (already rounded to their
published decimals), so a derived figure always agrees with the figures a
consumer sees.
"""

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
