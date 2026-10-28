"""Typer CLI entry point (`smolf`)."""

from datetime import datetime
from typing import Annotated

import numpy as np
import typer

from smol_frontier import __version__
from smol_frontier.classify import pipeline
from smol_frontier.classify.geotiff import write_classified_geotiff
from smol_frontier.classify.masks import cloud_fraction, valid_fraction
from smol_frontier.config import PROJECT_ROOT, settings
from smol_frontier.ingest import ingest_scene
from smol_frontier.quality import assess_quality
from smol_frontier.sources.stac import search_scenes

app = typer.Typer(help="smol-frontier CLI.", no_args_is_help=True)


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
    typer.echo(f"output: {path}")


if __name__ == "__main__":
    app()
