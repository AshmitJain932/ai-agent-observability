"""Guards that keep the normative JSON Schema, its lock, the changelog and the models in step."""

import json
import re

from jsonschema import Draft202012Validator

from aobs_server.model import KNOWN_MINOR, SUPPORTED_MAJOR, wire
from aobs_server.model.schema_lock import current
from support import SCHEMA_DIR, SCHEMA_PATH, load_schema


def test_schema_is_a_valid_2020_12_schema() -> None:
    Draft202012Validator.check_schema(load_schema())


def test_schema_matches_lock() -> None:
    locked = json.loads((SCHEMA_DIR / "v1" / "SCHEMA_LOCK").read_text())
    assert locked == current(), (
        "schema/v1/ingest.schema.json changed. Bump x-schema-version if needed, add a CHANGELOG "
        "entry, then run: uv run python -m aobs_server.model.schema_lock --write"
    )


def test_changelog_has_entry_for_locked_version() -> None:
    version = current()["schema_version"]
    changelog = (SCHEMA_DIR / "CHANGELOG.md").read_text()
    assert re.search(rf"^## {re.escape(version)}\b", changelog, re.MULTILINE), f"no CHANGELOG entry for {version}"


def test_server_version_policy_matches_schema_version() -> None:
    major, minor = (int(x) for x in current()["schema_version"].split("."))
    assert (major, minor) == (SUPPORTED_MAJOR, KNOWN_MINOR)


def test_model_patterns_are_copied_verbatim_from_schema() -> None:
    text = SCHEMA_PATH.read_text()
    patterns = [value for name, value in vars(wire).items() if name.endswith("_PATTERN")]
    assert len(patterns) >= 10
    for pattern in patterns:
        assert json.dumps(pattern) in text, f"pattern {pattern!r} not found in the JSON Schema"


def test_schema_patterns_only_use_dollar_as_anchor() -> None:
    # ecma_to_python relies on this.
    def walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("pattern"), str):
                assert r"\$" not in node["pattern"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(load_schema())
