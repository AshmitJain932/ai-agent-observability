"""API keys, authentication, the authorization decision point, and the tenancy CLI."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL

from aobs_server.auth import Principal, authorize, generate_key, hash_key, parse_bearer, require_scope
from aobs_server.auth.keys import KEY_PREFIX, is_well_formed
from aobs_server.auth.service import authenticate
from aobs_server.cli import main
from aobs_server.errors import Forbidden, NotFound, Unauthorized
from aobs_server.project_settings import ProjectSettings
from aobs_server.storage import tenancy
from tenants import make_key, make_project

# --------------------------------------------------------------------------- keys (pure)


def test_generated_keys_are_random_well_formed_and_hashed() -> None:
    keys = [generate_key() for _ in range(200)]
    assert len({k.secret for k in keys}) == 200
    for key in keys:
        assert key.secret.startswith(KEY_PREFIX) and is_well_formed(key.secret)
        assert len(key.secret) == len(KEY_PREFIX) + 43
        assert key.key_hash == hashlib.sha256(key.secret.encode()).digest()
        assert key.secret.startswith(key.prefix) and len(key.prefix) == len(KEY_PREFIX) + 4
        assert key.secret not in repr(key)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Bearer aobs_ik_short",
        "Bearer not_a_key_at_all_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "Bearer  " + "aobs_ik_" + "a" * 43,  # two spaces
        "Bearer " + "aobs_ik_" + "a" * 43 + " extra",
        "Bearer " + "aobs_ik_" + "a" * 42 + "\n",
    ],
)
def test_malformed_authorization_headers_are_unauthorized(header: str | None) -> None:
    with pytest.raises(Unauthorized) as info:
        parse_bearer(header)
    assert info.value.detail == "Missing or invalid API key"
    assert info.value.headers["WWW-Authenticate"].startswith("Bearer")


def test_bearer_scheme_is_case_insensitive() -> None:
    secret = generate_key().secret
    assert parse_bearer(f"bearer {secret}") == parse_bearer(f"Bearer {secret}") == hash_key(secret)


# --------------------------------------------------------------------------- authorize (pure)

PROJECT_A = uuid.uuid4()
PROJECT_B = uuid.uuid4()


def principal(*scopes: str, project: uuid.UUID = PROJECT_A) -> Principal:
    return Principal(key_id=uuid.uuid4(), project_id=project, scopes=frozenset(scopes), environment=None)


@pytest.mark.parametrize("action", ["ingest", "read", "delete"])
def test_other_projects_are_not_found_whatever_the_scopes(action: str) -> None:
    p = principal("ingest", "read", "admin")
    for target in (PROJECT_B, str(PROJECT_B), "not-a-uuid", ""):
        with pytest.raises(NotFound) as info:
            authorize(p, target, action)  # type: ignore[arg-type]
        assert info.value.status == 404
        assert str(PROJECT_B) not in info.value.detail


@pytest.mark.parametrize(
    ("scopes", "action", "allowed"),
    [
        (("ingest",), "ingest", True),
        (("ingest",), "read", False),
        (("ingest",), "delete", False),
        (("read",), "read", True),
        (("read",), "ingest", False),
        (("read",), "delete", False),
        (("admin",), "delete", True),
        (("admin",), "read", False),
        (("ingest", "read", "admin"), "delete", True),
    ],
)
def test_scopes_on_own_project(scopes: tuple[str, ...], action: str, allowed: bool) -> None:
    p = principal(*scopes)
    if allowed:
        assert authorize(p, str(PROJECT_A), action) == PROJECT_A  # type: ignore[arg-type]
        require_scope(p, action)  # type: ignore[arg-type]
    else:
        with pytest.raises(Forbidden):
            authorize(p, PROJECT_A, action)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- project settings


def test_project_settings_defaults_and_unknown_keys() -> None:
    assert ProjectSettings.from_db({}) == ProjectSettings()
    assert ProjectSettings().capture_ceiling == "metadata_only"
    assert ProjectSettings.from_db({"retention_days": 7, "future_setting": 1}).retention_days == 7
    assert ProjectSettings.from_db(None) == ProjectSettings()


# --------------------------------------------------------------------------- database


def test_authenticate_active_unknown_and_revoked_keys(engine: Engine) -> None:
    project = make_project(engine)
    key_id, secret = make_key(engine, project, ("ingest", "read"), environment="production")
    with engine.connect() as conn:
        p = authenticate(conn, f"Bearer {secret}")
        assert (p.key_id, p.project_id, p.scopes, p.environment) == (
            key_id,
            project,
            frozenset({"ingest", "read"}),
            "production",
        )
        with pytest.raises(Unauthorized):
            authenticate(conn, f"Bearer {generate_key().secret}")
    with engine.begin() as conn:
        assert tenancy.revoke_api_key(conn, key_id, datetime.now(UTC))
        assert not tenancy.revoke_api_key(conn, key_id, datetime.now(UTC))
    with engine.connect() as conn, pytest.raises(Unauthorized):
        authenticate(conn, f"Bearer {secret}")


def test_database_stores_only_hash_and_prefix(engine: Engine) -> None:
    project = make_project(engine)
    _, secret = make_key(engine, project)
    with engine.connect() as conn:
        dump = json.dumps([dict(r._mapping) for r in conn.execute(text("SELECT * FROM api_keys"))], default=str)
        stored_hash = conn.execute(text("SELECT key_hash FROM api_keys")).scalar_one()
    assert secret not in dump
    assert secret[len(KEY_PREFIX) + 4 :] not in dump
    assert bytes(stored_hash) == hash_key(secret)


def test_invalid_scopes_are_rejected_by_storage(engine: Engine) -> None:
    project = make_project(engine)
    with pytest.raises(ValueError, match="scopes"):
        make_key(engine, project, ("superuser",))


# --------------------------------------------------------------------------- CLI


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_tenancy_flow_never_leaks_the_secret(
    db_url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("AOBS_DATABASE_URL", db_url.render_as_string(hide_password=False))
    code, out, _ = _cli(capsys, "org", "create", "--name", "acme")
    assert code == 0
    org = json.loads(out)["organization_id"]
    code, out, _ = _cli(capsys, "project", "create", "--org", org, "--name", "support", "--capture-ceiling", "redacted")
    assert code == 0
    created = json.loads(out)
    project = created["project_id"]
    assert created["settings"]["capture_ceiling"] == "redacted"

    code, out, _ = _cli(capsys, "key", "create", "--project", project, "--name", "ci", "--scopes", "ingest,read")
    assert code == 0
    key = json.loads(out)
    secret = key["api_key"]
    assert is_well_formed(secret) and key["scopes"] == ["ingest", "read"]

    code, out, _ = _cli(capsys, "key", "list", "--project", project)
    assert code == 0
    listed = json.loads(out)
    assert [k["id"] for k in listed] == [key["key_id"]]
    assert secret not in out and "key_hash" not in out

    code, out, _ = _cli(capsys, "key", "revoke", "--key-id", key["key_id"])
    assert code == 0
    code, _, err = _cli(capsys, "key", "revoke", "--key-id", key["key_id"])
    assert code == 1 and "no active key" in err
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("argv", "code", "message"),
    [
        (("project", "create", "--org", "nope", "--name", "x"), 1, "not a valid id"),
        (("project", "create", "--org", str(uuid.uuid4()), "--name", "x"), 1, "does not exist"),
        (("project", "create", "--org", str(uuid.uuid4()), "--name", "x", "--retention-days", "0"), 2, "invalid"),
        (("key", "create", "--project", str(uuid.uuid4()), "--name", "x"), 1, "does not exist"),
        (("key", "create", "--project", str(uuid.uuid4()), "--name", "x", "--scopes", "root"), 2, "--scopes"),
        (("key", "create", "--project", str(uuid.uuid4()), "--name", "x", "--environment", "Prod!"), 2, "environment"),
    ],
)
def test_cli_errors(
    db_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    code: int,
    message: str,
) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", db_url.render_as_string(hide_password=False))
    got, _, err = _cli(capsys, *argv)
    assert got == code
    assert re.search(re.escape(message), err)
