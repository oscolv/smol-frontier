"""Daily reservoir water level from the public SIH record (station ENDHD).

Downloads the SIH CSV (Mexico's National Hydrometric Information System;
third-party data, credited in PRIOR_WORK.md),
parses Fecha / Nivel(m) / VolumenAlm(Mm3), and stores the bytes verbatim under
data/hydro/. Refreshes are validated against the local copy before replacing
it: wrong content, fewer days, an earlier end, missing days, or level
revisions over LEVEL_REVISION_TOL_M are rejected (kept local copy, non-zero
exit). First of three hydrology inputs for the exposed-bed correction.
"""

import csv
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import requests

from smol_frontier.config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LEVELS_PATH",
    "SIH_STATION",
    "SIH_URL",
    "LevelRow",
    "LevelsDownloadError",
    "LevelsError",
    "LevelsParseError",
    "LevelsUpdate",
    "LevelsValidationError",
    "RevisionCounts",
    "check_regression",
    "download_levels",
    "level_on",
    "load_levels",
    "parse_levels_bytes",
    "update_levels",
]

# WHY: the public SIH record for Endho — single download URL for the whole
#   daily series, so refreshes are one fetch with no date stitching.
SIH_URL = "https://sih.conagua.gob.mx/basedatos/Presas/ENDHD.csv"
SIH_STATION = "ENDHD"

# WHY: the SIH site sits behind a bot-protection layer — cookieless requests
#   get 307 redirect loops and 200s sometimes carry an HTML challenge page
#   instead of the CSV. One session keeps cookies across redirects; the
#   browser User-Agent, generous redirect/timeout budget, and backoff retries
#   get through it.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_REDIRECTS = 20
TIMEOUT_S = 60.0
MAX_ATTEMPTS = 4
RETRY_WAITS_S = (5.0, 10.0, 15.0)

# WHY: header contract of the SIH CSV — a few preamble lines, then the header
#   within the first 15 lines. Anything else (challenge page, error page) is
#   a failed download attempt, never parsed as data.
HEADER_PREFIX = "Estacion,Fecha,Nivel(m)"
HEADER_SEARCH_LINES = 15
COL_STATION = "Estacion"
COL_DATE = "Fecha"
COL_LEVEL = "Nivel(m)"
COL_VOLUME = "VolumenAlm(Mm3)"

# WHY: committed local copy of the SIH record — the pipeline reads levels
#   from here, never from the network, so runs are reproducible offline.
# HOW: repo layout anchored at PROJECT_ROOT; stored byte-for-byte (CRLF
#   included, see .gitattributes) with a .json sidecar of download provenance.
DEFAULT_LEVELS_PATH = PROJECT_ROOT / "data/hydro/ENDHD.csv"


class LevelsError(Exception):
    """Base error for SIH level download/parse/validate failures."""


class LevelsDownloadError(LevelsError):
    """The SIH CSV could not be fetched after every retry."""


class LevelsParseError(LevelsError):
    """Downloaded bytes are not the SIH ENDHD CSV."""


class LevelsValidationError(LevelsError):
    """New bytes fail validation against the local copy."""


@dataclass(frozen=True)
class LevelRow:
    """One daily record: date, level (m a.s.l.), stored volume (hm³).

    Empty or non-numeric level/volume parse as NaN; rows keep file order.
    """

    date: date
    level_m: float
    volume_hm3: float


@dataclass(frozen=True)
class RevisionCounts:
    """Changed values on days both copies share."""

    level_revisions: int
    volume_revisions: int


@dataclass(frozen=True)
class LevelsUpdate:
    """Result of an update run (written or --check)."""

    path: Path
    n_days: int
    first_day: date | None
    last_day: date | None
    last_day_with_level: date | None
    n_bytes: int
    sha256: str
    level_revisions: int
    volume_revisions: int
    written: bool


def _decode(data: bytes) -> str:
    """Decode SIH bytes (UTF-8, latin-1 fallback — never fails)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _looks_like_html(text: str) -> bool:
    """Challenge-page sniff: starts with "<" or contains "<html"."""
    return text.startswith("<") or "<html" in text


def _find_header_index(text: str) -> int | None:
    """Line index of the header, or None when absent."""
    for index, line in enumerate(text.splitlines()[:HEADER_SEARCH_LINES]):
        if line.startswith(HEADER_PREFIX):
            return index
    return None


def _parse_float(value: str) -> float:
    """Float or NaN when empty/non-numeric."""
    try:
        return float(value.strip())
    except ValueError:
        return math.nan


def parse_levels_bytes(data: bytes) -> list[LevelRow]:
    """Parse SIH CSV bytes into rows in file order.

    Raises LevelsParseError when the bytes are not the SIH ENDHD CSV: the
    HTML challenge page, a missing/mistyped header, or another station.
    """
    text = _decode(data)
    header_index = _find_header_index(text)
    if header_index is None:
        if _looks_like_html(text):
            msg = "not the SIH CSV: got the HTML challenge page instead"
        else:
            msg = (
                f"header line starting with {HEADER_PREFIX!r} not found "
                f"in the first {HEADER_SEARCH_LINES} lines"
            )
        raise LevelsParseError(msg)
    lines = text.splitlines()
    columns = next(csv.reader([lines[header_index]]))
    try:
        station_col = columns.index(COL_STATION)
        date_col = columns.index(COL_DATE)
        level_col = columns.index(COL_LEVEL)
        volume_col = columns.index(COL_VOLUME)
    except ValueError as exc:
        msg = f"header lacks a required column (need {COL_DATE}, {COL_LEVEL}, {COL_VOLUME}): {lines[header_index]!r}"
        raise LevelsParseError(msg) from exc
    rows: list[LevelRow] = []
    for lineno, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
        if not line.strip():
            continue
        fields = next(csv.reader([line]))
        if len(fields) <= max(station_col, date_col, level_col, volume_col):
            msg = f"short row on line {lineno}: {line!r}"
            raise LevelsParseError(msg)
        station = fields[station_col].strip()
        if station != SIH_STATION:
            msg = f"unexpected station {station!r} on line {lineno}, expected {SIH_STATION!r}"
            raise LevelsParseError(msg)
        try:
            day = date.fromisoformat(fields[date_col].strip())
        except ValueError as exc:
            msg = f"unparseable date {fields[date_col]!r} on line {lineno}"
            raise LevelsParseError(msg) from exc
        rows.append(
            LevelRow(
                date=day,
                level_m=_parse_float(fields[level_col]),
                volume_hm3=_parse_float(fields[volume_col]),
            )
        )
    return rows


def download_levels() -> bytes:
    """Fetch the SIH CSV, retrying bot-protection failures.

    One session (cookies survive redirects), up to 20 redirects, 60 s
    timeout, 4 attempts with 5/10/15 s waits. A 200 whose content is not
    the CSV counts as a failed attempt.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.max_redirects = MAX_REDIRECTS
    last_error = "no attempts made"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(SIH_URL, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
            elif _find_header_index(_decode(response.content)) is None:
                last_error = "response is not the SIH CSV"
                if _looks_like_html(_decode(response.content)):
                    last_error += " (HTML challenge page)"
            else:
                logger.info("SIH download ok: %d bytes", len(response.content))
                return response.content
        logger.info("SIH attempt %d/%d failed (%s)", attempt, MAX_ATTEMPTS, last_error)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_WAITS_S[attempt - 1])
    msg = f"SIH download failed after {MAX_ATTEMPTS} attempts: {last_error}"
    raise LevelsDownloadError(msg)


def _last_day_with_level(rows: list[LevelRow]) -> date | None:
    """Latest date carrying a non-NaN level, or None."""
    days = [row.date for row in rows if not math.isnan(row.level_m)]
    return max(days) if days else None


def check_regression(
    new_rows: list[LevelRow], old_rows: list[LevelRow], *, force: bool
) -> RevisionCounts:
    """Validate new rows against the local copy.

    Rejects fewer days, an earlier end, days missing from the new copy, and
    level revisions over LEVEL_REVISION_TOL_M (unless force). Revisions in
    other columns are accepted and counted for the report.
    """
    if len(new_rows) < len(old_rows):
        msg = f"new copy has fewer days ({len(new_rows)}) than local ({len(old_rows)})"
        raise LevelsValidationError(msg)
    new_days = {row.date for row in new_rows}
    old_days = {row.date for row in old_rows}
    if old_days and (not new_days or max(new_days) < max(old_days)):
        msg = f"new copy ends earlier ({max(new_days) if new_days else 'empty'}) than local ({max(old_days)})"
        raise LevelsValidationError(msg)
    missing = sorted(old_days - new_days)
    if missing:
        msg = f"new copy misses {len(missing)} local day(s), first: {missing[0]}"
        raise LevelsValidationError(msg)
    old_by_day = {row.date: row for row in old_rows}
    new_by_day = {row.date: row for row in new_rows}
    tol = settings.LEVEL_REVISION_TOL_M
    level_revisions = 0
    worst = 0.0
    volume_revisions = 0
    for day in sorted(old_days):
        old, new = old_by_day[day], new_by_day[day]
        if not math.isnan(old.level_m) and not math.isnan(new.level_m):
            change = abs(new.level_m - old.level_m)
            if change > tol:
                level_revisions += 1
                worst = max(worst, change)
        old_vol, new_vol = old.volume_hm3, new.volume_hm3
        if (math.isnan(old_vol) != math.isnan(new_vol)) or (
            not math.isnan(old_vol) and old_vol != new_vol
        ):
            volume_revisions += 1
    if level_revisions and not force:
        msg = (
            f"new copy revises Nivel(m) on {level_revisions} already-published "
            f"day(s) by more than {tol} m (worst: {worst:.3f} m); use --force to accept"
        )
        raise LevelsValidationError(msg)
    return RevisionCounts(level_revisions, volume_revisions)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sidecar_path(path: Path) -> Path:
    """Provenance sidecar next to the CSV (ENDHD.csv.json)."""
    return path.with_name(f"{path.name}.json")


def load_levels(path: Path | str | None = None) -> list[LevelRow]:
    """Load parsed rows from the local SIH copy (default: data/hydro)."""
    target = DEFAULT_LEVELS_PATH if path is None else Path(path)
    return parse_levels_bytes(target.read_bytes())


def level_on(day: date, path: Path | str | None = None) -> float | None:
    """Level for one exact day — no interpolation; None when missing/NaN."""
    for row in load_levels(path):
        if row.date == day:
            return None if math.isnan(row.level_m) else row.level_m
    return None


def update_levels(
    *,
    check: bool = False,
    force: bool = False,
    path: Path | str | None = None,
) -> LevelsUpdate:
    """Download, validate, and (unless check) store the SIH record.

    On any rejection the local copy is kept and LevelsError is raised.
    """
    target = DEFAULT_LEVELS_PATH if path is None else Path(path)
    data = download_levels()
    new_rows = parse_levels_bytes(data)
    old_rows = parse_levels_bytes(target.read_bytes()) if target.exists() else None
    revisions = (
        check_regression(new_rows, old_rows, force=force)
        if old_rows is not None
        else RevisionCounts(0, 0)
    )
    downloaded_utc = datetime.now(UTC).isoformat()
    last_with_level = _last_day_with_level(new_rows)
    if not check:
        _atomic_write_bytes(target, data)
        sidecar = {
            "url": SIH_URL,
            "downloaded_utc": downloaded_utc,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "n_days": len(new_rows),
            "last_day_with_level": (
                last_with_level.isoformat() if last_with_level else None
            ),
        }
        _atomic_write_bytes(
            _sidecar_path(target),
            (json.dumps(sidecar, indent=2) + "\n").encode("utf-8"),
        )
    return LevelsUpdate(
        path=target,
        n_days=len(new_rows),
        first_day=min((row.date for row in new_rows), default=None),
        last_day=max((row.date for row in new_rows), default=None),
        last_day_with_level=last_with_level,
        n_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        level_revisions=revisions.level_revisions,
        volume_revisions=revisions.volume_revisions,
        written=not check,
    )
