"""Shared test helpers: fixture loading and an ECMA-faithful JSON Schema validator."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError as SchemaError

from aobs_server.model import AssemblyConfig, loads_strict
from aobs_server.model.wire import ecma_to_python

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schema"
SCHEMA_PATH = SCHEMA_DIR / "v1" / "ingest.schema.json"
FIXTURES = SCHEMA_DIR / "fixtures"


def _ecma_pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Iterator[SchemaError]:
    # jsonschema uses Python re.search, where '$' also matches before a trailing newline.
    # JSON Schema patterns are ECMA-262, where it does not. Validate the ECMA way.
    if validator.is_type(instance, "string") and ecma_to_python(pattern).search(instance) is None:
        yield SchemaError(f"{instance!r} does not match {pattern!r}")


EcmaValidator = validators.extend(Draft202012Validator, {"pattern": _ecma_pattern})


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


SCHEMA_VALIDATOR = EcmaValidator(load_schema(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def schema_error_paths(payload: Any) -> list[str]:
    return ["/".join(str(p) for p in err.absolute_path) for err in SCHEMA_VALIDATOR.iter_errors(payload)]


def fixture_files(kind: str) -> list[Path]:
    return sorted((FIXTURES / kind).glob("*.json"))


def load_fixture(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = loads_strict(path.read_bytes())
    return data


def fixture_id(path: Path) -> str:
    return path.stem


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def assembly_config(expect: dict[str, Any]) -> AssemblyConfig:
    if "stale_after_seconds" in expect:
        return AssemblyConfig(stale_after=timedelta(seconds=expect["stale_after_seconds"]))
    return AssemblyConfig()


def fixture_path(kind: str, name: str) -> Path:
    path = FIXTURES / kind / f"{name}.json"
    assert path.exists(), path
    return path
