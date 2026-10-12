import copy
import json
from pathlib import Path

import pytest

from aobs_server.model import validate_batch
from support import fixture_files, fixture_id, fixture_path, load_fixture


@pytest.mark.parametrize("path", fixture_files("versioning"), ids=fixture_id)
def test_versioning_fixture(path: Path) -> None:
    fixture = load_fixture(path)
    expect = fixture["expect"]
    result = validate_batch(fixture["payload"])

    assert result.lenient is expect["lenient"]
    if "batch_error_paths" in expect:
        assert not result.ok
        assert sorted(e.path for e in result.batch_errors) == sorted(expect["batch_error_paths"])
        assert {e.type for e in result.batch_errors} == {"extra_forbidden"}
        return

    assert result.ok, result.batch_errors
    assert len(result.accepted) == expect["accepted"]
    assert len(result.rejected) == expect["rejected"]
    for index, paths in expect.get("rejected_paths", {}).items():
        rejected = next(r for r in result.rejected if r.index == int(index))
        assert sorted(e.path for e in rejected.errors) == sorted(paths)
    for index, extra in expect.get("event_extra", {}).items():
        accepted = next(a for a in result.accepted if a.index == int(index))
        assert dict(accepted.extra) == extra
    if "envelope_extra" in expect:
        assert dict(result.envelope_extra) == expect["envelope_extra"]


def _good_batch() -> dict:
    return copy.deepcopy(load_fixture(fixture_path("valid", "success_run"))["payload"])


def test_unknown_major_rejects_whole_batch() -> None:
    batch = _good_batch()
    batch["schema_version"] = "2.0"
    result = validate_batch(batch)
    assert not result.ok
    assert [e.type for e in result.batch_errors] == ["unsupported_schema_version"]
    assert result.accepted == ()


@pytest.mark.parametrize("version", ["1", "1.", "v1.0", "1.0.0", "01.0", "1.00", ""])
def test_malformed_version_rejected(version: str) -> None:
    batch = _good_batch()
    batch["schema_version"] = version
    result = validate_batch(batch)
    assert not result.ok
    assert any(e.path == "schema_version" for e in result.batch_errors)


def test_extra_fields_are_part_of_the_canonical_identity() -> None:
    # Two otherwise-identical events from a newer SDK that differ only in an unknown field are
    # different content (a conflict), not duplicates.
    batch = _good_batch()
    batch["schema_version"] = "1.1"
    first = copy.deepcopy(batch["events"][1])
    second = copy.deepcopy(first)
    first["future"] = 1
    second["future"] = 2
    batch["events"] = [first, second]
    result = validate_batch(batch)
    a, b = result.accepted
    assert a.digest() != b.digest()


def test_oversized_extras_are_dropped_and_reported() -> None:
    batch = _good_batch()
    batch["schema_version"] = "1.1"
    batch["events"][0]["future_blob"] = "x" * 5000
    result = validate_batch(batch)
    assert result.extra_dropped == (0,)
    assert dict(result.accepted[0].extra) == {}


def test_bytes_input_uses_strict_parser() -> None:
    raw = json.dumps(_good_batch()).replace('"duration_ms": 910.2', '"duration_ms": NaN')
    assert "NaN" in raw
    result = validate_batch(raw.encode())
    assert [e.type for e in result.batch_errors] == ["invalid_json"]


def test_non_object_payload() -> None:
    assert [e.type for e in validate_batch(b"[1, 2]").batch_errors] == ["dict_type"]


def test_too_many_events() -> None:
    batch = _good_batch()
    batch["events"] = batch["events"][:1] * 1001
    result = validate_batch(batch)
    assert any(e.path == "events" and e.type == "too_long" for e in result.batch_errors)
