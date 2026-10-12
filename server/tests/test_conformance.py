"""Shared fixtures must get the same verdict from the JSON Schema and from the Pydantic models.

Invalid fixtures name an expected path. Both validators must report at least one error at or
below that path. (The two report at slightly different depths — e.g. JSON Schema reports a
missing required field at the object, Pydantic at the field — so "at or below" is the contract.)
"""

import json
from pathlib import Path
from typing import Any

import pytest

from aobs_server.model import validate_batch
from support import fixture_files, fixture_id, fixture_path, load_fixture, schema_error_paths


def pydantic_error_paths(payload: Any) -> list[str]:
    result = validate_batch(payload)
    paths = [e.path for e in result.batch_errors]
    for rejected in result.rejected:
        paths.extend(e.path for e in rejected.errors)
    return paths


def _under(path: str, expected: str) -> bool:
    return expected == "" or path == expected or path.startswith(expected + "/")


@pytest.mark.parametrize("path", fixture_files("valid"), ids=fixture_id)
def test_valid_fixture_accepted_by_both(path: Path) -> None:
    payload = load_fixture(path)["payload"]
    assert schema_error_paths(payload) == []
    result = validate_batch(payload)
    assert result.ok, result.batch_errors
    assert result.rejected == ()
    assert len(result.accepted) == len(payload["events"])
    assert not result.lenient


@pytest.mark.parametrize("path", fixture_files("invalid"), ids=fixture_id)
def test_invalid_fixture_rejected_by_both_at_expected_path(path: Path) -> None:
    fixture = load_fixture(path)
    payload, expected = fixture["payload"], fixture["expect"]["path"]

    schema_paths = schema_error_paths(payload)
    model_paths = pydantic_error_paths(payload)

    assert schema_paths, "JSON Schema accepted an invalid fixture"
    assert model_paths, "Pydantic models accepted an invalid fixture"
    assert any(_under(p, expected) for p in schema_paths), (expected, schema_paths)
    assert any(_under(p, expected) for p in model_paths), (expected, model_paths)


def test_fixture_counts() -> None:
    # Guard against a glob silently matching nothing.
    assert len(fixture_files("valid")) >= 10
    assert len(fixture_files("invalid")) >= 40


def test_raw_bytes_and_parsed_payload_give_same_result() -> None:
    path = fixture_path("valid", "success_run")
    payload_bytes = json.dumps(json.loads(path.read_bytes())["payload"]).encode()
    from_bytes = validate_batch(payload_bytes)
    from_obj = validate_batch(load_fixture(path)["payload"])
    assert [a.digest() for a in from_bytes.accepted] == [a.digest() for a in from_obj.accepted]
