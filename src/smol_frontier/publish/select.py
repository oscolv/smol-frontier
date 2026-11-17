"""Run selection: read a run's manifest and pick a complete, clean run."""

import json
from pathlib import Path

from smol_frontier.publish.errors import PublishRefused


def _read_manifest(run_dir: Path) -> dict[str, object]:
    """The run's manifest; raise PublishRefused if unreadable or not an object."""
    path = run_dir / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers JSON and UTF-8 errors
        msg = f"run {run_dir.name} has an unreadable manifest.json: {exc}"[:300]
        raise PublishRefused(msg) from exc
    if not isinstance(manifest, dict):
        msg = f"run {run_dir.name} has a manifest.json that is not a JSON object"
        raise PublishRefused(msg)
    return manifest


def _is_complete(run_dir: Path) -> bool:
    """True for a run whose manifest is readable and says complete.

    WHY: `--latest` selection skips unreadable manifests, so one truncated run
    (e.g. an interrupted build) cannot block every `--latest` publish.
    """
    try:
        return _read_manifest(run_dir).get("status") == "complete"
    except PublishRefused:
        return False


def select_run(series_root: Path, run_id: str | None) -> Path:
    """Run directory to publish: ``run_id``, or the newest complete run.

    `--latest` selection (run_id None) skips unreadable manifests. Raises
    PublishRefused for an unknown run, no complete run, a run that is not
    complete or whose manifest is unreadable, or a run built from a dirty
    tree.
    """
    if run_id is None:
        complete = [
            path.parent
            for path in sorted(series_root.glob("*/manifest.json"))
            if _is_complete(path.parent)
        ]
        if not complete:
            msg = f"no complete run under {series_root}"
            raise PublishRefused(msg)
        run_dir = complete[-1]
    else:
        run_dir = series_root / run_id
        if not (run_dir / "manifest.json").is_file():
            msg = f"unknown run: {run_id}"
            raise PublishRefused(msg)
    manifest = _read_manifest(run_dir)
    if manifest.get("status") != "complete":
        msg = f"run {run_dir.name} is not complete (status {manifest.get('status')})"
        raise PublishRefused(msg)
    code = manifest.get("code")
    if not isinstance(code, dict) or code.get("dirty") is not False:
        msg = f"run {run_dir.name} was built from a dirty tree (code.dirty is true)"
        raise PublishRefused(msg)
    return run_dir
