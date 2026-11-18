"""Pure, deterministic figures derived from published scene entries.

Every function here works on contract entries (already rounded to their
published decimals), so a derived figure always agrees with the figures a
consumer sees.
"""

from collections.abc import Iterable
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


def _month_index(day: str) -> int:
    """A ``YYYY-MM-DD`` date as a running month count."""
    year, number = int(day[:4]), int(day[5:7])
    return year * 12 + number - 1


def _median(values: list[Decimal]) -> Decimal:
    """Ordinary median: the middle value, or the mean of the two middle ones."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def monthly_medians(scenes: list[dict[str, object]]) -> list[dict[str, object]]:
    """``[{month, median_ha, n_scenes}]``, one entry per calendar month.

    Months run contiguously from the first to the last scene month (HIDDEN
    scenes count toward the span). ``n_scenes`` counts the month's FULL and
    LOWER_BOUND scenes; ``median_ha`` is the ordinary median of their
    published ``hyacinth_corrected_ha`` rounded half up to 2 decimals, or
    None when the month has none. An empty ``scenes`` gives an empty list.
    """
    if not scenes:
        return []
    by_month: dict[int, list[Decimal]] = {}
    for scene in scenes:
        index = _month_index(str(scene["date"]))
        values = by_month.setdefault(index, [])
        if scene["display_status"] != HIDDEN:
            values.append(_decimal(scene["hyacinth_corrected_ha"]))
    entries: list[dict[str, object]] = []
    for index in range(min(by_month), max(by_month) + 1):
        values = by_month.get(index, [])
        median = (
            float(_median(values).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if values
            else None
        )
        year, month = divmod(index, 12)
        entries.append(
            {
                "month": f"{year:04d}-{month + 1:02d}",
                "median_ha": median,
                "n_scenes": len(values),
            }
        )
    return entries


def lower_median(scenes: list[dict[str, object]]) -> dict[str, object] | None:
    """The lower-median scene: the ⌈n/2⌉-th by ``hyacinth_corrected_ha``.

    Ties are broken by date, then item id, so the choice is deterministic and
    always a real scene. None for an empty list. Callers pass non-HIDDEN
    scenes only (their corrected figure is never null).
    """
    if not scenes:
        return None
    ordered = sorted(
        scenes,
        key=lambda scene: (
            _decimal(scene["hyacinth_corrected_ha"]),
            str(scene["date"]),
            str(scene["item_id"]),
        ),
    )
    return ordered[(len(ordered) + 1) // 2 - 1]


# WHY: the per-scene fields a same-month year entry carries — enough to
#   label the representative scene without a lookup in series.json.
_SAME_MONTH_SCENE_FIELDS = (
    "item_id",
    "date",
    "hyacinth_corrected_ha",
    "display_status",
)


def same_month(scenes: list[dict[str, object]]) -> dict[str, object] | None:
    """``{month, years}`` for the calendar month of the latest FULL scene.

    ``scenes`` are published entries sorted by date. ``years`` runs from the
    year of the first scene (HIDDEN and POOR included) to the year of that
    FULL scene, one ``{year, n_scenes, scene, featured}`` each: ``n_scenes`` counts
    the month's FULL and LOWER_BOUND scenes that year and ``scene`` is their
    lower median (``_SAME_MONTH_SCENE_FIELDS`` only), None when there is
    none. ``featured`` marks the current year plus the first, middle and last
    prior years, by scene count only (``_featured_indices``). None when no scene is FULL.
    """
    full = [scene for scene in scenes if scene["display_status"] == FULL]
    if not full:
        return None
    anchor = str(full[-1]["date"])
    month = int(anchor[5:7])
    by_year: dict[int, list[dict[str, object]]] = {}
    for scene in scenes:
        day = str(scene["date"])
        if int(day[5:7]) == month and scene["display_status"] != HIDDEN:
            by_year.setdefault(int(day[:4]), []).append(scene)
    years: list[dict[str, object]] = []
    for year in range(int(str(scenes[0]["date"])[:4]), int(anchor[:4]) + 1):
        shown = by_year.get(year, [])
        chosen = lower_median(shown)
        years.append(
            {
                "year": year,
                "n_scenes": len(shown),
                "scene": None
                if chosen is None
                else {field: chosen[field] for field in _SAME_MONTH_SCENE_FIELDS},
            }
        )
    featured = _featured_indices([year["n_scenes"] for year in years])
    for index, year in enumerate(years):
        year["featured"] = index in featured
    return {"month": month, "years": years}


def _featured_indices(n_scenes: list[int]) -> set[int]:
    """Indices of the featured years, given each year's scene count.

    WHY: a homepage shows four photos of the month, not one per year. The
    rule spans the record (first, middle and last year before the current
    one) and sees only scene counts, never hectares, so the choice cannot
    editorialize (ADR 0002). The current year (last index) always has a
    scene. Prior targets are index 0, n-1 and (n-1)//2 over the n prior
    years, resolved in that order; a target that is empty or already
    featured falls back to the next prior year with a scene (first:
    forward; last prior: backward; middle: nearest, ties to the earlier
    year), or stays empty when none is left.
    """
    current = len(n_scenes) - 1
    featured = {current}

    def pick(order: Iterable[int]) -> None:
        for index in order:
            if n_scenes[index] > 0 and index not in featured:
                featured.add(index)
                return

    middle = (current - 1) // 2
    pick(range(current))
    pick(range(current - 1, -1, -1))
    pick(sorted(range(current), key=lambda index: (abs(index - middle), index)))
    return featured
