"""Differential testing: the JSON Schema and the Pydantic models must give the same accept/reject
verdict for payloads near the valid fixtures. This catches drift between the two definitions.

Two layers:

1. **Exhaustive single mutations** (deterministic). For every node of every valid fixture: delete
   it, replace it with type-aware near-miss values, and add an unknown key to every object. A
   single change isolates the field under test, so a disagreement cannot be masked by some other
   invalid field. Planting a drift in either definition (e.g. accepting ``null`` or negative token
   counts) makes this fail — verified when the test was written.
2. **Random multi-mutations** (Hypothesis) for combinations the sweep does not reach.
"""

import copy
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from aobs_server.model import validate_batch
from support import fixture_files, fixture_id, load_fixture, schema_error_paths

Path = tuple[str | int, ...]


def _verdicts(payload: Any) -> tuple[bool, bool]:
    schema_ok = schema_error_paths(payload) == []
    result = validate_batch(payload)
    model_ok = result.ok and not result.rejected and not result.lenient
    return schema_ok, model_ok


def _near_misses(value: Any) -> list[Any]:
    """Values that are 'almost right' for the slot currently holding ``value``."""
    common: list[Any] = [None, True, [], {}]
    if isinstance(value, bool):
        return [None, 0, 1, "true", [], {}]
    if isinstance(value, int):
        return [*common, -1, 0, 1, value + 1, 1.5, float(value), 2**53, 2**63, str(value)]
    if isinstance(value, float):
        return [*common, -0.5, 0, 0.0, value * 1e12, str(value)]
    if isinstance(value, str):
        return [
            *common,
            0,
            "",
            value + "\n",
            " " + value,
            value.upper(),
            value * 20,
            value[:-1] or "x",
            "x" * 257,
            "x" * 4097,
            "a" * 32,
            "a" * 16,
            "2026-10-12T12:00:00Z",
            "2026-10-12T12:00:00+00:00",
        ]
    if isinstance(value, list):
        return [None, {}, "", [], [None], [{}], value * 40]
    if isinstance(value, dict):
        return [None, [], "", {}, 0]
    return [*common, 0, ""]  # value is None (e.g. parent_span_id)


def _nodes(node: Any, prefix: Path = ()) -> Iterator[tuple[Path, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield (*prefix, key), value
            yield from _nodes(value, (*prefix, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield (*prefix, i), value
            yield from _nodes(value, (*prefix, i))


def _objects(node: Any, prefix: Path = ()) -> Iterator[Path]:
    if isinstance(node, dict):
        yield prefix
    for path, value in _nodes(node, prefix):
        if isinstance(value, dict):
            yield path


def _get(node: Any, path: Path) -> Any:
    for part in path:
        node = node[part]
    return node


def single_mutations(payload: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path, value in list(_nodes(payload)):
        if path[:1] == ("events",) and len(path) == 1:
            continue
        label = "/".join(map(str, path))
        mutated = copy.deepcopy(payload)
        parent = _get(mutated, path[:-1])
        del parent[path[-1]]
        yield f"delete {label}", mutated
        for replacement in _near_misses(value):
            mutated = copy.deepcopy(payload)
            _get(mutated, path[:-1])[path[-1]] = replacement
            yield f"set {label} = {replacement!r:.40}", mutated
    for obj_path in list(_objects(payload)):
        for key in ("zz_unknown", "status", "value", "usage"):
            mutated = copy.deepcopy(payload)
            target = _get(mutated, obj_path)
            if key in target:
                continue
            target[key] = 1
            yield f"add {'/'.join(map(str, obj_path))}/{key}", mutated


@pytest.mark.parametrize("fixture_path", fixture_files("valid"), ids=fixture_id)
def test_exhaustive_single_mutations_agree(fixture_path: Any) -> None:
    payload = load_fixture(fixture_path)["payload"]
    disagreements = []
    checked = 0
    for label, mutated in single_mutations(payload):
        checked += 1
        schema_ok, model_ok = _verdicts(mutated)
        if schema_ok != model_ok:
            disagreements.append(
                f"{label}: schema={'accept' if schema_ok else 'reject'} models={'accept' if model_ok else 'reject'}"
            )
    assert checked > 50
    assert not disagreements, "\n".join(disagreements[:20])


# ---------------------------------------------------------------- random multi-mutations

PAYLOADS = [load_fixture(p)["payload"] for p in fixture_files("valid")]


@st.composite
def multi_mutated(draw: st.DrawFn) -> Any:
    payload = copy.deepcopy(draw(st.sampled_from(PAYLOADS)))
    for _ in range(draw(st.integers(2, 4))):
        nodes = [p for p, _ in _nodes(payload) if len(p) > 1 or p[0] != "events"]
        if not nodes:
            break
        path = draw(st.sampled_from(nodes))
        parent = _get(payload, path[:-1])
        if draw(st.booleans()):
            del parent[path[-1]]
        else:
            parent[path[-1]] = copy.deepcopy(draw(st.sampled_from(_near_misses(parent[path[-1]]))))
    return payload


@settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(multi_mutated())
def test_random_multi_mutations_agree(payload: Any) -> None:
    schema_ok, model_ok = _verdicts(payload)
    assert schema_ok == model_ok, schema_error_paths(payload)[:5]
