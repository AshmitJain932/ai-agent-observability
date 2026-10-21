"""aobs.safety under adversarial conditions: provider idempotency keys, operator acknowledgement of
stale actions, superseded results, a really paused process, concurrent recovery, crashes during
approval, racing store creation, clock jumps, and file hardening. Races are synchronised with pipes
and barriers, not sleeps; elapsed time is simulated with injectable clocks."""

from __future__ import annotations

import json
import os
import pickle
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import warnings
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import aobs
from aobs import _safety_store, safety
from aobs.export import InMemoryExporter
from sdk_support import flush

RULE = {"required_evidence": ["order_id", "amount"], "must_match": ["order_id"], "amount": "amount",
        "once_per": ["order_id"]}  # fmt: skip
POLICY = safety.parse_policy({"version": 1, "actions": {"issue_refund": RULE}})
REQUEST = {"order_id": "1042", "amount": 59.90}
KEY = "refund:T-1"
SECRET = bytes(range(32))  # test-only secrets; never printed
IDEM_SECRET = bytes(range(64, 96))
IDEM = safety.IdempotencyKeys(namespace="tests/local", secret=IDEM_SECRET)


class Clock:
    def __init__(self, now: float | None = None) -> None:
        self.now = time.time() if now is None else now

    def __call__(self) -> float:
        return self.now


def guard(store: safety.ActionStore | None = None, **options: Any) -> safety.ActionGuard:
    options.setdefault("idempotency", IDEM)
    return safety.ActionGuard(POLICY, store=store or safety.MemoryActionStore(), **options)


def sqlite(path: Path, **options: Any) -> safety.SqliteActionStore:
    return safety.SqliteActionStore(path, secret=SECRET, **options)


# --------------------------------------------------------------------------- provider idempotency keys


def test_idempotency_keys_are_stable_per_configuration_and_separate_namespaces() -> None:
    same = safety.IdempotencyKeys(namespace="tests/local", secret=IDEM_SECRET)
    other_env = safety.IdempotencyKeys(namespace="tests/staging", secret=IDEM_SECRET)
    other_secret = safety.IdempotencyKeys(namespace="tests/local", secret=bytes(range(100, 132)))
    key = IDEM.key("issue_refund", KEY)
    assert same.key("issue_refund", KEY) == key and same.key_id == IDEM.key_id  # retries, workers, restarts
    assert len({key, other_env.key("issue_refund", KEY), other_secret.key("issue_refund", KEY)}) == 3
    assert len({IDEM.key_id, other_env.key_id, other_secret.key_id}) == 3
    assert IDEM.key("issue_refund", "refund:T-2") != key and IDEM.key("other_action", KEY) != key
    assert "T-1" not in key and key.startswith("aobs-") and len(key) == 45
    assert IDEM_SECRET.hex() not in repr(IDEM) and "tests/local" in repr(IDEM)
    with pytest.raises(TypeError):
        pickle.dumps(IDEM)


def test_the_same_configuration_gives_the_same_key_in_another_process() -> None:
    code = (
        "import os, sys\nfrom aobs import safety\n"
        "keys = safety.IdempotencyKeys(namespace='tests/local', secret=bytes.fromhex(os.environ['T_IDEM']))\n"
        "sys.stdout.write(keys.key('issue_refund', 'refund:T-1'))\n"
    )
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                           env={**os.environ, "T_IDEM": IDEM_SECRET.hex()})  # fmt: skip
    assert child.stdout == IDEM.key("issue_refund", KEY)


@pytest.mark.parametrize(
    ("namespace", "secret", "message"),
    [
        ("", IDEM_SECRET, "namespace"),
        (" tests", IDEM_SECRET, "namespace"),
        ("tests local", IDEM_SECRET, "namespace"),
        ("t" * 129, IDEM_SECRET, "namespace"),
        (None, IDEM_SECRET, "namespace"),
        ("tests/local", b"short", "at least 32 bytes"),
        ("tests/local", IDEM_SECRET.hex(), "at least 32 bytes"),
        ("tests/local", b"\x00" * 32, "not random"),
        ("tests/local", b"ab" * 32, "not random"),
    ],
)
def test_invalid_idempotency_configuration_is_refused(namespace: Any, secret: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message) as error:
        safety.IdempotencyKeys(namespace=namespace, secret=secret)
    assert IDEM_SECRET.hex() not in str(error.value)


def test_an_action_key_without_idempotency_configuration_fails_closed(tmp_path: Path) -> None:
    calls: list[str] = []
    for store in (safety.MemoryActionStore(), sqlite(tmp_path / "a.db")):
        unconfigured = safety.ActionGuard(POLICY, store=store)
        result = unconfigured.run("issue_refund", REQUEST, REQUEST, lambda ctx: calls.append("x"), action_key=KEY)
        assert (result.decision.reason_codes, result.decision.rule) == (
            ("idempotency_unconfigured",),
            "guard.idempotency",
        )
        assert unconfigured.inspect("issue_refund", KEY) is None  # nothing was reserved
        with pytest.raises(TypeError, match="idempotency"):
            unconfigured.protect(lambda order_id, amount: None, action="issue_refund", evidence=lambda: REQUEST,
                                 action_key=KEY)  # fmt: skip
    assert calls == []
    with pytest.raises(TypeError, match="IdempotencyKeys"):
        safety.ActionGuard(POLICY, idempotency="tests/local")  # type: ignore[arg-type]


def test_an_action_is_never_continued_under_another_idempotency_configuration(tmp_path: Path) -> None:
    rotated = safety.IdempotencyKeys(namespace="tests/local", secret=bytes(range(100, 132)))
    store = sqlite(tmp_path / "a.db")
    guard(store).run("issue_refund", REQUEST, REQUEST, lambda ctx: (_ for _ in ()).throw(safety.ActionNotPerformed()),
                     action_key=KEY)  # fmt: skip
    keys: list[str] = []
    rotated_guard = guard(store, idempotency=rotated)
    retry = rotated_guard.run("issue_refund", REQUEST, REQUEST, lambda ctx: keys.append(ctx.idempotency_key),
                              action_key=KEY)  # fmt: skip
    assert retry.decision.reason_codes == ("idempotency_config_changed",) and keys == []
    assert guard(store).run("issue_refund", REQUEST, REQUEST, lambda ctx: keys.append(ctx.idempotency_key),
                            action_key=KEY).executed  # fmt: skip
    assert keys == [IDEM.key("issue_refund", KEY)]  # the original configuration's key

    lookups: list[str] = []
    guard(store).run("issue_refund", {"order_id": "2077", "amount": 5.0}, {"order_id": "2077", "amount": 5.0},
                     lambda ctx: (_ for _ in ()).throw(TimeoutError()), action_key="refund:T-2")  # fmt: skip
    with pytest.raises(safety.IdempotencyConfigMismatch):
        rotated_guard.reconcile("issue_refund", "refund:T-2",
                                verify=lambda ctx: lookups.append(ctx.idempotency_key) or "FAILED")  # fmt: skip
    assert lookups == [] and guard(store).inspect("issue_refund", "refund:T-2").state == "UNKNOWN"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- stale actions need a statement


def stale_unknown(store: safety.ActionStore, clock: Clock) -> safety.ActionGuard:
    """Declare KEY stale while its attempt runs; the attempt then ends without an outcome."""

    operator = guard(store, clock=clock)

    def execute(ctx: safety.ActionContext) -> str:
        clock.now += 3600
        assert operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND").resolution == "stale"  # type: ignore[union-attr]
        raise KeyboardInterrupt  # the attempt goes away without a word (recorded as UNKNOWN by its finally)

    with pytest.raises(KeyboardInterrupt):
        guard(store, clock=clock).run("issue_refund", REQUEST, REQUEST, execute, action_key=KEY)
    return operator


def test_resolving_a_stale_action_as_failed_requires_the_operators_statement(tmp_path: Path) -> None:
    clock = Clock()
    store = sqlite(tmp_path / "a.db")
    worker, operator = guard(store, clock=clock), guard(store, clock=clock)
    calls: list[str] = []

    def paused(ctx: safety.ActionContext) -> str:
        clock.now += 3600
        assert operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND").resolution == "stale"  # type: ignore[union-attr]
        for statement in (False, "yes", 1):
            with pytest.raises(ValueError, match="original_stopped=True"):
                operator.resolve("issue_refund", KEY, "FAILED", original_stopped=statement)  # type: ignore[arg-type]
        record = operator.inspect("issue_refund", KEY)
        assert record is not None and (record.state, record.resolution) == ("UNKNOWN", "stale")  # unchanged
        blocked = operator.run("issue_refund", REQUEST, REQUEST, lambda c: calls.append("B"), action_key=KEY)
        assert blocked.previous_outcome == "UNKNOWN"
        resolved = operator.resolve("issue_refund", KEY, "FAILED", original_stopped=True)
        assert (resolved.state, resolved.resolution) == ("FAILED", "manual_stopped")  # recorded, not verified
        return "done"

    first = worker.run("issue_refund", REQUEST, REQUEST, paused, action_key=KEY)
    assert calls == [] and first.superseded and first.final_outcome == "SUCCEEDED"  # it did happen: conflict kept
    assert operator.inspect("issue_refund", KEY).resolution == "conflict"  # type: ignore[union-attr]


def test_a_success_or_a_finished_attempts_unknown_needs_no_statement(tmp_path: Path) -> None:
    store = sqlite(tmp_path / "a.db")
    g = guard(store)
    g.run("issue_refund", REQUEST, REQUEST, lambda c: (_ for _ in ()).throw(TimeoutError()), action_key=KEY)
    assert g.inspect("issue_refund", KEY).resolution == "none"  # type: ignore[union-attr] # its own attempt ended
    assert g.resolve("issue_refund", KEY, "FAILED").resolution == "manual"
    clock = Clock()
    other = sqlite(tmp_path / "b.db")
    operator = stale_unknown(other, clock)
    record = operator.inspect("issue_refund", KEY)
    assert record is not None and (record.state, record.resolution) == ("UNKNOWN", "none")  # it ended by itself
    assert operator.resolve("issue_refund", KEY, "FAILED").resolution == "manual"  # its attempt is over
    third = sqlite(tmp_path / "c.db")
    clock = Clock()
    worker, operator = guard(third, clock=clock), guard(third, clock=clock)

    def paused(ctx: safety.ActionContext) -> str:
        clock.now += 3600
        operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND")
        assert operator.resolve("issue_refund", KEY, "SUCCEEDED").resolution == "manual"  # blocks: no statement
        return "done"

    assert worker.run("issue_refund", REQUEST, REQUEST, paused, action_key=KEY).final_outcome == "SUCCEEDED"


# --------------------------------------------------------------------------- superseded results


def test_a_contradicting_adapter_reports_the_stored_outcome(sdk: InMemoryExporter) -> None:
    store = safety.MemoryActionStore()
    operator = guard(store)

    def contradicts(order_id: str, amount: float) -> str:
        operator.reconcile("issue_refund", KEY, verify=lambda c: "SUCCEEDED")  # the provider has it
        raise safety.ActionNotPerformed("declined")  # the adapter says otherwise

    tool = guard(store).protect(contradicts, action="issue_refund", evidence=lambda: REQUEST, action_key=KEY)
    with pytest.raises(safety.ActionBlocked) as blocked:
        tool(order_id="1042", amount=59.90)
    result = blocked.value.result
    assert (result.outcome, result.attempt_outcome, result.stored_outcome) == ("FAILED", "FAILED", "SUCCEEDED")
    assert result.superseded and result.final_outcome == "SUCCEEDED"
    assert "SUCCEEDED" in str(blocked.value) and "superseded: this attempt FAILED, stored SUCCEEDED" in str(
        blocked.value
    )
    flush()
    (span,) = [e for e in sdk.events() if e["type"] == "span_end" and e["name"] == "action issue_refund"]
    attributes = span["attributes"]
    assert attributes["aobs.safety.final_outcome"] == "SUCCEEDED" and attributes["aobs.safety.superseded"] is True
    assert (attributes["aobs.safety.attempt_outcome"], attributes["aobs.safety.stored_outcome"]) == (
        "FAILED",
        "SUCCEEDED",
    )


def test_an_unsuperseded_result_reports_its_stored_outcome_too() -> None:
    result = guard().run("issue_refund", REQUEST, REQUEST, lambda c: "done", action_key=KEY)
    assert (result.final_outcome, result.stored_outcome, result.superseded) == ("SUCCEEDED", "SUCCEEDED", False)
    unkeyed = safety.ActionGuard(safety.parse_policy({"version": 1, "actions": {"issue_refund": {
        **RULE, "once_per": []}}})).run("issue_refund", REQUEST, REQUEST, lambda c: "done")  # fmt: skip
    assert unkeyed.stored_outcome is None and unkeyed.final_outcome == "SUCCEEDED"  # nothing was stored


# --------------------------------------------------------------------------- clock jumps


@pytest.mark.parametrize("jump", [+3600.0, -3600.0])
def test_clock_jumps_never_release_an_action_by_themselves(tmp_path: Path, jump: float) -> None:
    clock = Clock()
    store = sqlite(tmp_path / "a.db")
    worker, operator = guard(store, clock=clock), guard(store, clock=clock)
    seen: dict[str, Any] = {}

    def execute(ctx: safety.ActionContext) -> str:
        clock.now += jump  # NTP step or VM resume; the attempt is still running
        seen["live"] = operator.is_live(operator.inspect("issue_refund", KEY))  # type: ignore[arg-type]
        seen["record"] = operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND")
        with pytest.raises(ValueError):
            operator.resolve("issue_refund", KEY, "FAILED")  # never without the operator's statement
        seen["other"] = operator.run("issue_refund", REQUEST, REQUEST, lambda c: "B", action_key=KEY)
        return "done"

    first = worker.run("issue_refund", REQUEST, REQUEST, execute, action_key=KEY)
    if jump > 0:  # looks stale: declared UNKNOWN, never FAILED
        assert not seen["live"] and (seen["record"].state, seen["record"].resolution) == ("UNKNOWN", "stale")
    else:  # backwards: stays live longer
        assert seen["live"] and seen["record"].state == "EXECUTING"
    assert seen["other"].outcome == "NOT_EXECUTED"
    assert first.final_outcome == "SUCCEEDED" and not first.superseded  # the real outcome settles it


# --------------------------------------------------------------------------- a really paused process

PAUSED_CHILD = textwrap.dedent(
    """
    import json, os, sqlite3, sys
    from aobs import safety

    store_path, ledger, keymode = sys.argv[1:4]
    policy = safety.parse_policy({"version": 1, "actions": {"issue_refund": %s}})
    idempotency = safety.IdempotencyKeys(namespace="tests/local", secret=bytes.fromhex(os.environ["T_IDEM"]))
    store = safety.SqliteActionStore(store_path, secret=bytes.fromhex(os.environ["T_STORE"]))
    guard = safety.ActionGuard(policy, store=store, idempotency=idempotency)

    def refund(ctx):
        print("ready", flush=True)  # EXECUTING is committed; the request is not sent yet
        sys.stdin.readline()  # paused until the parent lets it continue
        key = ctx.idempotency_key if keymode == "idempotent" else ctx.action_id
        db = sqlite3.connect(ledger, timeout=30)
        with db:
            db.execute("INSERT OR IGNORE INTO refunds VALUES (?)", (key,))  # the provider: one refund per key
        db.close()
        return "done"

    request = {"order_id": "1042", "amount": 59.90}
    r = guard.run("issue_refund", request, request, refund, action_key="refund:T-1")
    print(json.dumps({"outcome": r.final_outcome, "superseded": r.superseded, "stored": r.stored_outcome}))
    """
) % json.dumps(RULE).replace("true", "True")


@pytest.mark.parametrize(("keymode", "refunds"), [("idempotent", 1), ("ignores-keys", 2)])
def test_a_really_paused_process_resumes_after_a_takeover(tmp_path: Path, keymode: str, refunds: int) -> None:
    store_path, ledger = tmp_path / "actions.db", tmp_path / "ledger.db"
    sqlite(store_path)
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute("CREATE TABLE refunds (key TEXT PRIMARY KEY)")
    script = tmp_path / "child.py"
    script.write_text(PAUSED_CHILD)
    child = subprocess.Popen(
        [sys.executable, str(script), str(store_path), str(ledger), keymode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "T_STORE": SECRET.hex(), "T_IDEM": IDEM_SECRET.hex()},
    )  # fmt: skip
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        later = Clock(time.time() + 3600)  # the operator looks after stale_after_s, without waiting for it
        operator = guard(sqlite(store_path), clock=later)
        stale = operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND")
        assert stale is not None and (stale.state, stale.resolution) == ("UNKNOWN", "stale")
        operator.resolve("issue_refund", KEY, "FAILED", original_stopped=True)  # a wrong statement

        def send(ctx: safety.ActionContext) -> str:
            key = ctx.idempotency_key if keymode == "idempotent" else ctx.action_id
            with closing(sqlite3.connect(ledger, timeout=30)) as db, db:
                db.execute("INSERT OR IGNORE INTO refunds VALUES (?)", (key,))
            return "done"

        takeover = operator.run("issue_refund", REQUEST, REQUEST, send, action_key=KEY)
        assert takeover.outcome == "SUCCEEDED"
        out, err = child.communicate("go\n", timeout=60)  # the original resumes
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0, err
    original = json.loads(out.strip().splitlines()[-1])
    assert original == {"outcome": "SUCCEEDED", "superseded": True, "stored": "SUCCEEDED"}
    record = operator.inspect("issue_refund", KEY)
    assert record is not None and record.attempt_id == takeover.action_id  # the newer record stands
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT count(*) FROM refunds").fetchone()[0] == refunds  # the provider decides


# --------------------------------------------------------------------------- concurrent recovery and creation


def test_operators_reconciling_at_the_same_moment_apply_one_change(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "a.db"
    worker = guard(sqlite(path), clock=clock)
    outcomes: list[safety.ActionRecord | None] = []

    def execute(ctx: safety.ActionContext) -> str:
        clock.now += 3600
        barrier = threading.Barrier(4)

        def operator() -> None:
            g = guard(sqlite(path), clock=clock)
            barrier.wait()
            outcomes.append(g.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND"))

        threads = [threading.Thread(target=operator) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return "done"

    first = worker.run("issue_refund", REQUEST, REQUEST, execute, action_key=KEY)
    versions = {r.version for r in outcomes if r is not None}
    assert len(outcomes) == 4 and {r.state for r in outcomes if r is not None} == {"UNKNOWN"}
    assert len(versions) == 1  # one update applied; the others saw it
    assert first.final_outcome == "SUCCEEDED" and not first.superseded


def test_a_crash_while_waiting_for_approval_blocks_until_the_lease_expires(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    sqlite(path)
    policy = {"version": 1, "actions": {"issue_refund": {**RULE, "approval_above": "1"}}}
    code = textwrap.dedent(
        f"""
        import os
        from aobs import safety
        guard = safety.ActionGuard(
            safety.parse_policy({policy!r}),
            store=safety.SqliteActionStore({str(path)!r}, secret=bytes.fromhex(os.environ["T_STORE"])),
            idempotency=safety.IdempotencyKeys(namespace="tests/local", secret=bytes.fromhex(os.environ["T_IDEM"])),
            lease_s=300,
        )
        request = {{"order_id": "1042", "amount": 59.9}}
        guard.run("issue_refund", request, request, lambda c: "done", action_key="refund:T-1",
                  approver=lambda c: os._exit(9))  # the process dies while a person is asked
        """
    )
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**os.environ, "T_STORE": SECRET.hex(), "T_IDEM": IDEM_SECRET.hex()})  # fmt: skip
    assert child.returncode == 9, child.stderr
    calls: list[str] = []
    approving = safety.parse_policy(policy)

    def attempt(clock: Clock) -> safety.ActionResult:
        g = safety.ActionGuard(approving, store=sqlite(path), idempotency=IDEM, clock=clock)
        return g.run("issue_refund", REQUEST, REQUEST, lambda c: calls.append("x"), action_key=KEY,
                     approver=lambda c: True)  # fmt: skip

    early = attempt(Clock())
    assert (early.outcome, early.previous_outcome, calls) == ("NOT_EXECUTED", "RESERVED", [])
    late = attempt(Clock(time.time() + 301))  # after the lease: it never executed, so it may be replaced
    assert late.outcome == "SUCCEEDED" and calls == ["x"]


@pytest.mark.parametrize("same_secret", [True, False])
def test_stores_created_at_the_same_moment(tmp_path: Path, same_secret: bool) -> None:
    other = SECRET if same_secret else bytes(range(100, 132))
    expected = ["opened", "opened"] if same_secret else ["opened", "refused: the secret does not match"]
    for round_ in range(60):  # the race used to fail about 1 in 20 rounds with "database is locked"
        path, barrier, results = tmp_path / f"a{round_}.db", threading.Barrier(2), []

        def open_with(
            secret: bytes, path: Path = path, barrier: threading.Barrier = barrier, results: list[str] = results
        ) -> None:
            barrier.wait()
            try:
                safety.SqliteActionStore(path, secret=secret)
                results.append("opened")
            except safety.StoreConfigurationError as error:
                results.append("refused: the secret does not match" if "does not match" in str(error) else str(error))
            except Exception as error:  # e.g. sqlite3.OperationalError: database is locked
                results.append(f"{type(error).__name__}: {error}")

        threads = [threading.Thread(target=open_with, args=(s,)) for s in (SECRET, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(results) == expected, round_


# --------------------------------------------------------------------------- the store's files


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="POSIX symbolic links")
def test_symbolic_links_and_non_files_are_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    sqlite(real)
    (tmp_path / "link.db").symlink_to(real)
    (tmp_path / "dangling.db").symlink_to(tmp_path / "nowhere.db")
    for name in ("link.db", "dangling.db"):
        with pytest.raises(safety.StoreConfigurationError, match="symbolic link"):
            sqlite(tmp_path / name)
    assert not (tmp_path / "nowhere.db").exists()  # nothing was created through the link
    (tmp_path / "dir.db").mkdir()
    with pytest.raises(safety.StoreConfigurationError, match="not a regular file"):
        sqlite(tmp_path / "dir.db")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_shared_files_are_reported_not_trusted(tmp_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fresh = sqlite(tmp_path / "fresh.db")  # created 0600: no warning
    assert fresh.private is True
    shared = tmp_path / "shared.db"
    shared.touch()
    shared.chmod(0o644)
    with pytest.warns(safety.UnsafePermissionsWarning, match="shared.db can be read or written by other users"):
        store = sqlite(shared)
    assert store.private is False and oct(shared.stat().st_mode & 0o777) == "0o644"  # reported, not changed
    sqlite(tmp_path / "wal.db")
    Path(f"{tmp_path / 'wal.db'}-wal").touch(0o666)
    os.chmod(f"{tmp_path / 'wal.db'}-wal", 0o666)
    with pytest.warns(safety.UnsafePermissionsWarning, match="wal.db-wal"):
        assert sqlite(tmp_path / "wal.db").private is False


class _NoWal:
    """A connection whose WAL switch is refused, as on file systems without shared memory."""

    def __init__(self, db: sqlite3.Connection) -> None:
        object.__setattr__(self, "_db", db)

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
            return self._db.execute("PRAGMA journal_mode")  # stays "delete"
        return self._db.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._db, name, value)


def test_a_store_that_cannot_use_wal_refuses_to_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connect = sqlite3.connect
    monkeypatch.setattr(_safety_store.sqlite3, "connect", lambda *a, **k: _NoWal(connect(*a, **k)))
    with pytest.raises(safety.StoreConfigurationError, match="could not enable WAL"):
        sqlite(tmp_path / "a.db")


@pytest.mark.parametrize("secret", [b"\x00" * 32, b"a" * 32, b"0123456" * 5])
def test_secrets_that_cannot_be_random_are_refused(tmp_path: Path, secret: bytes) -> None:
    with pytest.raises(safety.StoreConfigurationError, match="not random") as error:
        safety.SqliteActionStore(tmp_path / "a.db", secret=secret)
    assert secret.hex() not in str(error.value)


def test_secrets_never_reach_the_trace_or_errors(sdk: InMemoryExporter, tmp_path: Path) -> None:
    store = sqlite(tmp_path / "a.db")
    g = guard(store)
    with aobs.run("ticket"):
        g.run("issue_refund", REQUEST, REQUEST, lambda c: (_ for _ in ()).throw(TimeoutError()), action_key=KEY)
        g.reconcile("issue_refund", KEY, verify=lambda c: "PENDING")
        g.resolve("issue_refund", KEY, "SUCCEEDED")
    flush()
    text = "".join(body.decode() for body in sdk.bodies)
    for value in (SECRET.hex(), IDEM_SECRET.hex(), IDEM.key("issue_refund", KEY), IDEM.key_id, "tests/local", KEY):
        assert value not in text
    assert SECRET.hex() not in repr(store) and IDEM_SECRET.hex() not in repr(IDEM)
