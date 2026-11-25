"""Smoke tests: package imports and the `smolf version` CLI works."""

import subprocess
import sysconfig

import smol_frontier


def test_import() -> None:
    assert smol_frontier.__version__ == "0.12.0"


def test_smolf_version() -> None:
    scripts = sysconfig.get_path("scripts")
    proc = subprocess.run(
        [f"{scripts}/smolf", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert smol_frontier.__version__ in proc.stdout


def test_failures_go_to_stderr_with_stdout_left_for_results(
    monkeypatch, tmp_path
) -> None:
    """Every failing command reports on stderr, so stdout stays parseable."""
    import responses
    from typer.testing import CliRunner

    from smol_frontier import cli, paths
    from smol_frontier.hydro.levels import SIH_URL

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli, "search_scenes", lambda *args, **kwargs: [])
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    runner = CliRunner()
    cases = {
        "classify, no scene": (["classify", "2026-10-13"], "no scenes found"),
        "levels show, missing day": (["levels", "show", "1900-01-01"], "no level"),
        "series build, bad args": (
            ["series", "build", "--extend", "--resume", "x"],
            "error: --extend cannot be combined",
        ),
    }
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, SIH_URL, body="<html>challenge</html>", status=200)
        cases["levels update, SIH down"] = (
            ["levels", "update"],
            "error: SIH download failed after 4 attempts",
        )
        for name, (args, message) in cases.items():
            result = runner.invoke(cli.app, args)
            assert result.exit_code != 0, name
            assert message in result.stderr, name
            assert result.stdout == "", name
