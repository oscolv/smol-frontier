"""Typer CLI entry point (`smolf`)."""

from datetime import datetime
from typing import Annotated

import numpy as np
import typer

from smol_frontier import __version__
from smol_frontier.classify.masks import cloud_fraction, valid_fraction
from smol_frontier.ingest import ingest_scene
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


if __name__ == "__main__":
    app()
