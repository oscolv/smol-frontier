"""Typer CLI entry point (`smolf`)."""

import math
from datetime import datetime
from typing import Annotated

import numpy as np
import typer

from smol_frontier import __version__
from smol_frontier.classify import pipeline
from smol_frontier.classify.geotiff import write_classified_geotiff
from smol_frontier.classify.masks import cloud_fraction, valid_fraction
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.hydro.bed import correct_bed
from smol_frontier.hydro.curve import build_curve
from smol_frontier.hydro.flood_map import FloodMapError, build_flood_map
from smol_frontier.hydro.levels import (
    DEFAULT_LEVELS_PATH,
    LevelsError,
    load_levels,
    update_levels,
)
from smol_frontier.ingest import ingest_scene
from smol_frontier.quality import assess_quality
from smol_frontier.sources.stac import search_scenes

app = typer.Typer(help="smol-frontier CLI.", no_args_is_help=True)
levels_app = typer.Typer(help="Reservoir water levels from the SIH record.")
app.add_typer(levels_app, name="levels")
hydro_app = typer.Typer(help="Hydrology inputs for the exposed-bed correction.")
app.add_typer(hydro_app, name="hydro")


@app.callback()
def main() -> None:
    """smol-frontier CLI."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def fetch(
    day: Annotated[
        datetime, typer.Argument(..., formats=["%Y-%m-%d"], help="Day (YYYY-MM-DD).")
    ],
    max_cloud: Annotated[
        float, typer.Option("--max-cloud", help="Max eo:cloud_cover.")
    ] = 100.0,
) -> None:
    """Download one day's scene into the local cache and print its stats."""
    scenes = search_scenes(day.date(), day.date(), max_cloud)
    if not scenes:
        typer.echo(f"no scenes found for {day.date()}")
        raise typer.Exit(1)
    ing = ingest_scene(scenes[0])
    scl = ing.bands["SCL"]
    b02, b04, b11 = (ing.reflectance(band) for band in ("B02", "B04", "B11"))
    water = ing.reflectance("B03")[(scl == 6) & ing.inside]
    median = f"{float(np.median(water)):.4f}" if water.size else "n/a"
    typer.echo(f"scene: {ing.scene.id}")
    typer.echo(f"backend: {ing.scene.source}")
    typer.echo(f"baseline: {ing.scene.processing_baseline or 'unknown'}")
    typer.echo(f"offset: {ing.offset}")
    typer.echo(f"valid_fraction: {valid_fraction(scl, b02, b11, b04, ing.inside):.4f}")
    typer.echo(f"cloud_fraction: {cloud_fraction(scl, b02, b11, b04, ing.inside):.4f}")
    typer.echo(f"median_b03_water: {median}")


@app.command()
def classify(
    day: Annotated[
        datetime, typer.Argument(..., formats=["%Y-%m-%d"], help="Day (YYYY-MM-DD).")
    ],
) -> None:
    """Classify one day's scene, print the summary, write a GeoTIFF."""
    scenes = search_scenes(day.date(), day.date(), 100.0)
    if not scenes:
        typer.echo(f"no scenes found for {day.date()}")
        raise typer.Exit(1)
    ing = ingest_scene(scenes[0])
    result = pipeline.classify(ing)
    ha = result.ha_per_class
    bed = correct_bed(result.class_map, ing.transform, ing.crs, day.date())
    out_dir = PROJECT_ROOT / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ing.scene.id.replace('/', '_')}_classified.tif"
    write_classified_geotiff(path, result.class_map, ing.transform, ing.crs)
    nir_thr = "none" if result.nir_thr_used is None else f"{result.nir_thr_used:.4f}"
    typer.echo(f"scene: {result.scene_id}")
    typer.echo(f"water_ha: {ha[settings.CLASS_WATER]:.2f}")
    typer.echo(f"dense_ha: {ha[settings.CLASS_DENSE_HYACINTH]:.2f}")
    typer.echo(f"interface_ha: {ha[settings.CLASS_INTERFACE]:.2f}")
    typer.echo(f"soil_ha: {ha[settings.CLASS_BARE_SOIL]:.2f}")
    if bed.applied:
        typer.echo(
            f"hyacinth_total_ha: {bed.hyacinth_corrected_ha:.2f} "
            f"(raw {result.hyacinth_total_ha:.2f})"
        )
        typer.echo(
            f"pct_reservoir: {bed.pct_corrected:.2f} (raw {result.pct_reservoir:.2f})"
        )
    else:
        typer.echo(f"hyacinth_total_ha: {result.hyacinth_total_ha:.2f}")
        typer.echo(f"pct_reservoir: {result.pct_reservoir:.2f}")
    typer.echo(f"reservoir_area_ha: {result.reservoir_area_ha:.2f}")
    typer.echo(f"valid_fraction: {result.valid_fraction:.4f}")
    typer.echo(f"cloud_fraction: {result.cloud_fraction:.4f}")
    typer.echo(f"gmm1_sep: {result.gmm1_sep:.4f}")
    typer.echo(f"gmm2_sep: {result.gmm2_sep:.4f}")
    typer.echo(f"nir_thr_used: {nir_thr}")
    typer.echo(f"nir_water_frac: {result.nir_water_frac:.4f}")
    typer.echo(f"n_rescued: {result.n_rescued}")
    typer.echo(f"n_substrate: {result.n_substrate}")
    quality = assess_quality(result)
    typer.echo(f"light: {quality.light}")
    typer.echo(f"degrading_flags: {','.join(quality.degrading_flags) or 'none'}")
    typer.echo(f"info_flags: {','.join(quality.info_flags) or 'none'}")
    typer.echo(f"regime: {quality.regime or 'clean'}")
    if not quality.publishable_pct:
        typer.echo("PERCENT NOT PUBLISHABLE")
    typer.echo("EXPOSED BED")
    if bed.applied:
        typer.echo(f"bed_level_m: {bed.level_m:.2f}")
        typer.echo(f"bed_margin_m: {bed.margin_m:.2f}")
        typer.echo(f"bed_expected_ha: {bed.expected_bed_ha:.2f}")
        typer.echo(f"bed_removed_ha: {bed.removed_ha:.2f}")
        typer.echo(f"bed_corrected_total_ha: {bed.hyacinth_corrected_ha:.2f}")
        typer.echo(f"bed_corrected_dense_ha: {bed.dense_corrected_ha:.2f}")
        typer.echo(f"bed_corrected_interface_ha: {bed.interface_corrected_ha:.2f}")
        typer.echo(f"bed_corrected_pct: {bed.pct_corrected:.2f}")
    else:
        typer.echo(f"NO CORRECTION: {bed.reason}")
    typer.echo(f"output: {path}")


@levels_app.command("update")
def levels_update(
    check: Annotated[
        bool, typer.Option("--check", help="Download and validate without writing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Accept level revisions over tolerance.")
    ] = False,
) -> None:
    """Refresh the local SIH water-level record."""
    try:
        report = update_levels(check=check, force=force)
    except LevelsError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(1)
    typer.echo(f"days: {report.n_days}")
    typer.echo(f"first_day: {report.first_day}")
    typer.echo(f"last_day: {report.last_day}")
    typer.echo(f"last_day_with_level: {report.last_day_with_level or 'none'}")
    typer.echo(f"bytes: {report.n_bytes}")
    typer.echo(f"sha256: {report.sha256}")
    typer.echo(f"level_revisions: {report.level_revisions}")
    typer.echo(f"volume_revisions: {report.volume_revisions}")
    if check:
        typer.echo("check only: not written")
    else:
        typer.echo(f"output: {report.path}")


@levels_app.command("show")
def levels_show(
    day: Annotated[
        datetime, typer.Argument(..., formats=["%Y-%m-%d"], help="Day (YYYY-MM-DD).")
    ],
) -> None:
    """Print the SIH level for one exact day (no interpolation)."""
    if not DEFAULT_LEVELS_PATH.exists():
        typer.echo("no local levels file; run `smolf levels update` first")
        raise typer.Exit(1)
    match = next((row for row in load_levels() if row.date == day.date()), None)
    if match is None or math.isnan(match.level_m):
        typer.echo(f"no level for {day.date()}")
        raise typer.Exit(1)
    volume = "n/a" if math.isnan(match.volume_hm3) else f"{match.volume_hm3}"
    typer.echo(f"date: {match.date}")
    typer.echo(f"level_m: {match.level_m:.2f}")
    typer.echo(f"volume_hm3: {volume}")


@hydro_app.command("curve")
def hydro_curve() -> None:
    """Build the stage–area curve from the SIH record and write it."""
    if not DEFAULT_LEVELS_PATH.exists():
        typer.echo("no local levels file; run `smolf levels update` first")
        raise typer.Exit(1)
    report = build_curve()
    typer.echo(f"pairs: {report.n_pairs}")
    typer.echo(f"level_min: {report.level_min:.2f}")
    typer.echo(f"level_max: {report.level_max:.2f}")
    typer.echo(f"s_factor: {report.s_factor}")
    typer.echo(f"area_at_2018_km2: {report.area_at_2018_km2:.4f}")
    typer.echo(f"volume_at_2018_hm3: {report.volume_at_2018_hm3:.3f}")
    typer.echo(f"volume_at_2003_16_hm3: {report.volume_at_2003_16_hm3:.3f}")
    for stage, err in report.model_errors_km2.items():
        typer.echo(f"model_error_{stage}_km2: {err:.4f}")
    typer.echo(f"output: {report.csv_path}")


@hydro_app.command("flood-map")
def hydro_flood_map() -> None:
    """Build the per-pixel flood-stage map and write it."""
    try:
        report = build_flood_map()
    except FloodMapError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(1)
    typer.echo(f"pixels: {report.n_pixels}")
    typer.echo(f"stage_min: {report.stage_min:.3f}")
    typer.echo(f"stage_max: {report.stage_max:.3f}")
    typer.echo(f"frac_at_h_min: {report.frac_at_h_min:.4f}")
    typer.echo(f"sink_fill_ha: {report.sink_fill_ha:.2f}")
    for stage, (map_ha, curve_ha) in report.controls.items():
        typer.echo(f"area_{stage:g}_map_ha: {map_ha:.2f} curve_ha: {curve_ha:.2f}")
    typer.echo(f"output: {report.tif_path}")


if __name__ == "__main__":
    app()
