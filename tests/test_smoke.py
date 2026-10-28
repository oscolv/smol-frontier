"""Smoke tests: package imports and the `smolf version` CLI works."""

import subprocess
import sysconfig

import smol_frontier


def test_import() -> None:
    assert smol_frontier.__version__ == "0.1.0"


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
