"""Typer CLI entry point (`smolf`)."""

import typer

from smol_frontier import __version__

app = typer.Typer(help="smol-frontier CLI.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """smol-frontier CLI."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
