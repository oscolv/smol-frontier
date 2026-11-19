"""Issue #60: choose the gamma of the published true-colour photos.

Renders the featured same-month photos of a published contract under the
fixed reflectance window (``TRUE_COLOUR_MIN``..``TRUE_COLOUR_MAX``) and
several gamma values, so they can be compared side by side before a value
goes into ``config.py``. The photos are clipped to the reservoir with the
validity alpha of contract 3.2.0 (inside the polygon and any band above
``TRUE_COLOUR_VALID_MIN``).

The bands are read again from each item's COGs through
``publish.truecolour.read_true_colour`` (same radiometry and BOA-offset
guard as ``smolf publish``). The grid is rebuilt from the ``latest.json``
photo's ``bounds`` and pixel size. Every run checks that rebuild: at the
gamma the contract was published with (``--published-gamma``) the render
must reproduce the contract's own clipped photo (alpha and RGB agreement
are printed per year). No production code path gains a parameter
or changes behaviour; the gamma is applied here only.

Writes, under the output folder (git-ignored):

- ``montage.png``: one row per gamma (in ``--gammas`` order), one column per
  featured year (ascending), on the site's light background;
- ``brightness.csv``: mean RGB brightness (% of 255) over the valid pixels
  inside the reservoir, per gamma and year.

Usage::

    uv run python experiments/truecolour_gamma.py \
        --contract-dir outputs/publish --gammas 1.0 1.4 1.6 2.2
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

from smol_frontier import paths
from smol_frontier.config import settings
from smol_frontier.publish import truecolour as tc

# WHY: the site's page background, so transparent pixels look as on the web.
BACKGROUND = np.array([234, 237, 231], dtype=np.uint8)
GUTTER = 16


def display_grid(
    contract: Path, latest: dict
) -> tuple[rasterio.Affine, tuple[int, int]]:
    """The ``latest.json`` photo's web-Mercator grid (transform, shape)."""
    (south, west), (north, east) = latest["image"]["bounds"]
    with rasterio.open(contract / latest["image"]["path"]) as src:
        height, width = src.height, src.width
    bounds = transform_bounds("EPSG:4326", tc.DISPLAY_CRS, west, south, east, north)
    return from_bounds(*bounds, width, height), (height, width)


def render(reflectance: np.ndarray, valid: np.ndarray, gamma: float) -> np.ndarray:
    """(h, w, 3) uint8: fixed window, then ``x ** (1/gamma)``, background outside."""
    low, high = settings.TRUE_COLOUR_MIN, settings.TRUE_COLOUR_MAX
    x = np.clip((np.nan_to_num(reflectance, nan=low) - low) / (high - low), 0.0, 1.0)
    rgb = np.rint(x ** (1.0 / gamma) * 255.0).astype(np.uint8).transpose(1, 2, 0)
    return np.where(valid[..., None], rgb, BACKGROUND)


def check_against_published(
    contract: Path, year: dict, reflectance: np.ndarray, valid: np.ndarray, gamma: float
) -> str:
    """Agreement of the render at ``gamma`` with the contract's clipped photo."""
    with rasterio.open(contract / year["image"]["path"]) as src:
        published = src.read()
    opaque = published[3] > 0
    both = opaque & valid
    rgb = render(reflectance, valid, gamma).transpose(2, 0, 1)
    diff = np.abs(rgb[:, both].astype(int) - published[:3, both].astype(int))
    return (
        f"alpha agrees on {(opaque == valid).mean():.3%} of pixels, "
        f"mean |RGB diff| {diff.mean():.2f}/255"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="True-colour gamma comparison (#60).")
    parser.add_argument(
        "--contract-dir",
        type=Path,
        default=None,
        help="Published contract (default: smol_frontier.paths.publish_dir()).",
    )
    parser.add_argument(
        "--backend",
        default="earth-search",
        help="STAC backend of the contract's run (smolf publish reads the same).",
    )
    parser.add_argument(
        "--published-gamma",
        type=float,
        default=settings.TRUE_COLOUR_GAMMA,
        help="Gamma the contract was published with (1.0 before contract 3.2.0).",
    )
    parser.add_argument("--gammas", type=float, nargs="+", default=[1.0, 1.4, 1.6, 2.2])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output folder (default: <outputs>/truecolour_gamma).",
    )
    args = parser.parse_args()
    contract = args.contract_dir or paths.publish_dir()
    out = args.output_dir or paths.outputs_dir() / "truecolour_gamma"
    out.mkdir(parents=True, exist_ok=True)

    latest = json.loads((contract / "latest.json").read_text())
    transform, shape = display_grid(contract, latest)
    # zero reflectance is valid everywhere, so this alpha is the polygon alone
    inside = tc.to_rgba(np.zeros((3, *shape)), transform)[3] > 0
    years = [y for y in latest["same_month"]["years"] if y["featured"]]

    tiles: dict[tuple[float, int], np.ndarray] = {}
    rows = []
    for year in years:
        photo = tc.read_true_colour(
            args.backend, year["scene"]["item_id"], transform, shape
        )
        refl = photo.reflectance
        valid = inside & (
            np.nan_to_num(refl, nan=-np.inf) > settings.TRUE_COLOUR_VALID_MIN
        ).any(axis=0)
        check = check_against_published(
            contract, year, refl, valid, args.published_gamma
        )
        print(f"{year['year']} vs published photo: {check}")
        for gamma in args.gammas:
            tile = render(refl, valid, gamma)
            tiles[gamma, year["year"]] = tile
            brightness = round(float(tile[valid].mean()) / 2.55, 1)
            rows.append(
                {
                    "year": year["year"],
                    "item_id": year["scene"]["item_id"],
                    "gamma": gamma,
                    "mean_brightness_pct": brightness,
                }
            )
            print(f"{year['year']} gamma 1/{gamma}: {brightness} %")

    height, width = shape
    montage = np.full(
        (
            len(args.gammas) * (height + GUTTER) + GUTTER,
            len(years) * (width + GUTTER) + GUTTER,
            3,
        ),
        243,
        np.uint8,
    )
    for r, gamma in enumerate(args.gammas):
        for c, year in enumerate(years):
            top, left = GUTTER + r * (height + GUTTER), GUTTER + c * (width + GUTTER)
            montage[top : top + height, left : left + width] = tiles[
                gamma, year["year"]
            ]
    with rasterio.open(
        out / "montage.png",
        "w",
        driver="PNG",
        width=montage.shape[1],
        height=montage.shape[0],
        count=3,
        dtype="uint8",
    ) as dst:
        dst.write(montage.transpose(2, 0, 1))
    with (out / "brightness.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out / 'montage.png'} and {out / 'brightness.csv'}")


if __name__ == "__main__":
    main()
