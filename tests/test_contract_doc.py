"""docs/contract.md must document every property of the packaged schemas.

Walks the three contract schemas recursively (properties, $defs, if/then/
else, items, anyOf...) and checks each property name appears in the
contract document as inline code — bare (`cover_band`) or as the last
segment of a dotted path (`map.bounds`, `files[].sha256`) — so the docs
cannot drift from the schemas.
"""

import json
import re
from importlib import resources
from pathlib import Path

import pytest

CONTRACT_DOC = Path(__file__).resolve().parents[1] / "docs" / "contract.md"
SCHEMAS = ("latest", "series", "publish")


def _schema(name: str) -> dict:
    text = (
        resources.files("smol_frontier")
        .joinpath("schemas", f"{name}.schema.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _property_names(node: object) -> set[str]:
    """Every key of every ``properties`` object anywhere in the schema."""
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in node.values():
            names |= _property_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _property_names(value)
    return names


def _documented(prop: str, text: str) -> bool:
    """True when ``prop`` appears as inline code, bare or as a path's tail."""
    return re.search(rf"`(?:[A-Za-z0-9_\[\]]+\.)*{re.escape(prop)}`", text) is not None


def test_documented_matches_bare_and_dotted_names() -> None:
    assert _documented("bounds", "see `map.bounds` here")
    assert _documented("sha256", "`files[].sha256`")
    assert _documented("date", "`date`")
    assert not _documented("ok", "`run_counts.okay`")
    assert not _documented("png", "map.png without backticks")


def test_property_walk_finds_nested_fields() -> None:
    names = _property_names(_schema("latest"))
    assert {"cover_band", "frac_publishable", "levels_sha256", "bounds"} <= names
    assert {"path", "sha256"} <= _property_names(_schema("publish"))


@pytest.mark.parametrize("name", SCHEMAS)
def test_contract_doc_lists_every_schema_property(name: str) -> None:
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    missing = sorted(
        prop for prop in _property_names(_schema(name)) if not _documented(prop, text)
    )
    assert not missing, f"{name}.schema.json properties missing from docs: {missing}"
