"""aobs.safety action stores: the reservation rule, both stores' semantics, durability across
restarts, coordination between threads and between processes, and recovery after a crash that
happened after (or before) the side effect."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pickle
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from aobs import safety

RULE = {"required_evidence": ["order_id", "amount"], "must_match": ["order_id"], "amount": "amount",
        "amount_at_most_evidence": True, "once_per": ["order_id"]}  # fmt: skip
POLICY = safety.parse_policy({"version": 1, "actions": {"issue_refund": RULE}})
ORDER = {"order_id": "1042", "amount": 59.90}
REQUEST = {"order_id": "1042", "amount": 59.90}
SECRET = bytes(range(32))  # a fixed test secret; never printed
ONCE = ("order_id",)
IDEM_SECRET = bytes(range(64, 96))  # a fixed test secret for provider idempotency keys; never printed
IDEM = safety.IdempotencyKeys(namespace="tests/local", secret=IDEM_SECRET)


def Guard(*args: Any, **options: Any) -> safety.ActionGuard:  # stands in for the class
    """An ActionGuard with the tests' idempotency configuration, unless one is given."""
    options.setdefault("idempotency", IDEM)
    return safety.ActionGuard(*args, **options)


def sqlite(path: Path, **options: Any) -> safety.SqliteActionStore:
    return safety.SqliteActionStore(path, secret=SECRET, **options)


def record(key: str = "k1", state: Any = "RESERVED", *, once: str | None = "o1", digest: str = "d1",
           attempt: str = "a1", lease: float = 1e12) -> safety.ActionRecord:  # fmt: skip
    return safety.ActionRecord("issue_refund", key, attempt, state, digest, once, lease)


STORES: dict[str, Callable[[Path], Any]] = {
    "memory": lambda tmp: safety.MemoryActionStore(),
    "sqlite": lambda tmp: sqlite(tmp / "actions.db"),
}


# --------------------------------------------------------------------------- the reservation rule


@pytest.mark.parametrize(
    ("same_key", "same_once", "expected"),
    [
        (None, None, (None, None)),
        (record(state="SUCCEEDED"), None, ("same_key", None)),
        (record(state="EXECUTING", lease=0), None, ("same_key", None)),  # executing never expires
        (record(state="UNKNOWN", lease=0), None, ("same_key", None)),
        (record(state="RESERVED"), None, ("same_key", None)),
        (record(state="RESERVED", lease=0), None, (None, None)),  # expired, never executed: replaced
        (record(state="FAILED"), None, (None, None)),
        (record(state="FAILED", digest="other"), None, ("same_key", None)),  # same key, different request
        (None, record("k2", "SUCCEEDED"), ("same_once", None)),
        (None, record("k2", "RESERVED", lease=0), (None, "same_once")),
    ],
)
def test_the_reservation_rule(same_key: Any, same_once: Any, expected: tuple[str | None, str | None]) -> None:
    claim = record(attempt="a2")
    named = {"same_key": same_key, "same_once": same_once, None: None}
    assert safety.reservation_conflict(claim, same_key, same_once, now=1000.0) == tuple(named[e] for e in expected)


# --------------------------------------------------------------------------- both stores


@pytest.mark.parametrize("kind", sorted(STORES))
def test_store_semantics(kind: str, tmp_path: Path) -> None:
    store = STORES[kind](tmp_path)
    assert store.kind == kind and store.durable is (kind == "sqlite")
    assert store.reserve(record(), now=1.0, once_per=ONCE) is None
    blocking = store.reserve(record(attempt="a2"), now=2.0, once_per=ONCE)
    assert blocking is not None and blocking.attempt_id == "a1"
    assert store.reserve(record("k2", attempt="a3"), now=2.0, once_per=ONCE).attempt_id == "a1"  # type: ignore[union-attr] # once_key
    assert store.update("issue_refund", "k1", "a2", expect=("RESERVED",), state="EXECUTING", now=3.0)[0] is False
    moved, current = store.update("issue_refund", "k1", "a1", expect=("RESERVED",), state="EXECUTING", now=3.0)
    assert moved and current.state == "EXECUTING" and current.created_at == 1.0 and current.updated_at == 3.0
    store.release("issue_refund", "k1", "a1")  # only a reservation that never executed can be released
    assert store.get("issue_refund", "k1").state == "EXECUTING"  # type: ignore[union-attr]
    store.update("issue_refund", "k1", "a1", expect=("EXECUTING",), state="FAILED", now=4.0, error_type="X")
    assert (
        store.reserve(record("k2", attempt="a3"), now=5.0, once_per=ONCE) is None
    )  # a failed action no longer blocks the order
    assert store.get("issue_refund", "k2").state == "RESERVED"  # type: ignore[union-attr]
    store.release("issue_refund", "k2", "a3")
    assert store.get("issue_refund", "k2") is None
    assert (
        store.reserve(record(attempt="a4"), now=6.0, once_per=ONCE) is None
    )  # the failed key itself, same request: retried
    assert store.get("issue_refund", "k1").attempt_id == "a4"  # type: ignore[union-attr]


@pytest.mark.parametrize("kind", sorted(STORES))
def test_an_expired_reservation_of_another_key_is_evicted(kind: str, tmp_path: Path) -> None:
    store = STORES[kind](tmp_path)
    store.reserve(record("k1", lease=10.0), now=1.0, once_per=ONCE)
    assert store.reserve(record("k2", attempt="a2"), now=11.0, once_per=ONCE) is None
    assert store.get("issue_refund", "k1") is None
    assert store.update("issue_refund", "k1", "a1", expect=("RESERVED",), state="EXECUTING", now=12.0) == (False, None)


# --------------------------------------------------------------------------- SQLite


def test_sqlite_survives_a_restart_and_holds_no_raw_values(tmp_path: Path) -> None:
    path = tmp_path / "actions.db"
    first = Guard(POLICY, store=sqlite(path))
    assert (
        first.run("issue_refund", REQUEST, ORDER, lambda ctx: "done", action_key="refund:T-7731").outcome == "SUCCEEDED"
    )
    calls: list[str] = []
    restarted = Guard(POLICY, store=sqlite(path))  # a new process
    again = restarted.run("issue_refund", REQUEST, ORDER, lambda ctx: calls.append("x"), action_key="refund:T-7731")
    assert (again.outcome, again.previous_outcome, calls) == ("NOT_EXECUTED", "SUCCEEDED", [])
    other_key = restarted.run("issue_refund", REQUEST, ORDER, lambda ctx: calls.append("x"), action_key="refund:T-9")
    assert other_key.decision.rule == "actions.issue_refund.once_per" and calls == []  # the same order
    raw = b"".join(f.read_bytes() for f in tmp_path.iterdir())
    for value in (b"T-7731", b"1042", b"59.9", IDEM.key("issue_refund", "refund:T-7731").encode()):
        assert value not in raw, value


def test_sqlite_refuses_an_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "actions.db"
    sqlite(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="schema version 99"):
        sqlite(path)


def test_the_unique_index_backs_the_once_per_rule(tmp_path: Path) -> None:
    path = tmp_path / "actions.db"
    sqlite(path).reserve(record(), now=1.0, once_per=ONCE)
    with closing(sqlite3.connect(path)) as db, db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO aobs_safety_actions VALUES "
            "('issue_refund', 'k9', 'a9', 'RESERVED', 'd', 'o1', 0, 'none', NULL, 0, 0, NULL, 1, NULL)"
        )


@pytest.mark.parametrize("same_key", [True, False])
def test_threads_with_their_own_guards_and_store_objects_run_the_action_once(tmp_path: Path, same_key: bool) -> None:
    path, barrier, executed = tmp_path / "actions.db", threading.Barrier(8, timeout=60), []
    sqlite(path)  # created before the race

    def execute(ctx: safety.ActionContext) -> str:
        executed.append(ctx.action_id)
        time.sleep(0.05)
        return "done"

    def worker(n: int) -> None:
        guard = Guard(POLICY, store=sqlite(path))
        barrier.wait()
        key = "refund:T-1" if same_key else f"refund:T-{n}"  # different keys, same order: once_per
        guard.run("issue_refund", REQUEST, ORDER, execute, action_key=key)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(executed) == 1


# --------------------------------------------------------------------------- processes and crashes

CHILD = textwrap.dedent(
    """
    import json, os, sqlite3, sys, time
    from aobs import safety

    store_path, ledger, go, mode, key = sys.argv[1:6]
    policy = safety.parse_policy({"version": 1, "actions": {"issue_refund": %s}})
    secret = bytes.fromhex(os.environ["AOBS_TEST_STORE_SECRET"])
    idem_secret = bytes.fromhex(os.environ["AOBS_TEST_IDEM_SECRET"])
    idempotency = safety.IdempotencyKeys(namespace="tests/local", secret=idem_secret)
    store = safety.SqliteActionStore(store_path, secret=secret)
    guard = safety.ActionGuard(policy, store=store, idempotency=idempotency)
    while not os.path.exists(go):  # start together
        time.sleep(0.005)

    def refund(ctx):
        if mode == "crash_before":
            os._exit(17)  # the process dies before the payments API is called
        with sqlite3.connect(ledger, timeout=30) as db:  # the payments API makes the refund
            db.execute("INSERT INTO refunds VALUES (?, ?)", (ctx.idempotency_key, ctx.request["order_id"]))
        if mode == "crash_after":
            os._exit(17)  # the process dies before the guard records the outcome
        time.sleep(0.2)
        return "done"

    request = {"order_id": "1042", "amount": 59.90}
    result = guard.run("issue_refund", request, request, refund, action_key=key)
    print(json.dumps({"outcome": result.outcome, "previous": result.previous_outcome}))
    """
) % json.dumps(RULE).replace("true", "True")


def ledger(path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS refunds (idempotency_key TEXT, order_id TEXT)")
        return list(db.execute("SELECT * FROM refunds"))


def run_children(tmp_path: Path, jobs: list[tuple[str, str]]) -> list[subprocess.CompletedProcess[str]]:
    script, go = tmp_path / "child.py", tmp_path / "go"
    script.write_text(CHILD)
    store_path, ledger_path = tmp_path / "actions.db", tmp_path / "ledger.db"
    sqlite(store_path)
    ledger(ledger_path)
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(store_path), str(ledger_path), str(go), mode, key],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "AOBS_TEST_STORE_SECRET": SECRET.hex(), "AOBS_TEST_IDEM_SECRET": IDEM_SECRET.hex()},
        )
        for mode, key in jobs
    ]
    time.sleep(0.3)
    go.touch()
    done = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        done.append(subprocess.CompletedProcess(p.args, p.returncode, out, err))
    return done


@pytest.mark.parametrize("same_key", [True, False])
def test_processes_sharing_the_store_make_one_refund(tmp_path: Path, same_key: bool) -> None:
    jobs = [("slow", "refund:T-1" if same_key else f"refund:T-{n}") for n in range(4)]
    done = run_children(tmp_path, jobs)
    assert all(p.returncode == 0 for p in done), [p.stderr for p in done]
    outcomes = sorted(json.loads(p.stdout)["outcome"] for p in done)
    assert outcomes == ["NOT_EXECUTED"] * 3 + ["SUCCEEDED"]
    assert len(ledger(tmp_path / "ledger.db")) == 1


def test_a_crash_after_the_side_effect_is_recovered_without_a_second_refund(tmp_path: Path) -> None:
    (crashed,) = run_children(tmp_path, [("crash_after", "refund:T-1")])
    assert crashed.returncode == 17 and len(ledger(tmp_path / "ledger.db")) == 1
    guard = Guard(POLICY, store=sqlite(tmp_path / "actions.db"))  # the restart
    assert guard.inspect("issue_refund", "refund:T-1").state == "EXECUTING"  # type: ignore[union-attr]
    calls: list[str] = []
    again = guard.run("issue_refund", REQUEST, ORDER, lambda ctx: calls.append("x"), action_key="refund:T-1")
    assert (again.outcome, again.previous_outcome, calls) == ("NOT_EXECUTED", "EXECUTING", [])  # not retried

    def lookup(ctx: safety.ActionContext) -> str:  # read-only, by the stable idempotency key
        keys = [k for k, _ in ledger(tmp_path / "ledger.db")]
        return "SUCCEEDED" if ctx.idempotency_key in keys else "NOT_FOUND"

    settled = guard.reconcile("issue_refund", "refund:T-1", verify=lookup)
    assert settled is not None and (settled.state, settled.resolution) == ("SUCCEEDED", "verified")
    assert len(ledger(tmp_path / "ledger.db")) == 1


def test_a_crash_before_the_side_effect_stays_unknown_until_a_person_resolves_it(tmp_path: Path) -> None:
    (crashed,) = run_children(tmp_path, [("crash_before", "refund:T-1")])
    assert crashed.returncode == 17 and ledger(tmp_path / "ledger.db") == []
    soon = Guard(POLICY, store=sqlite(tmp_path / "actions.db"))
    still = soon.reconcile("issue_refund", "refund:T-1", verify=lambda ctx: "NOT_FOUND")
    assert still is not None and still.state == "EXECUTING"  # it may only be paused: still blocking
    with pytest.raises(ValueError, match="still considered running"):
        soon.resolve("issue_refund", "refund:T-1", "FAILED")
    later = time.time() + 3600  # past stale_after_s (900 s): the crashed process is not coming back
    guard = Guard(POLICY, store=sqlite(tmp_path / "actions.db"), clock=lambda: later)
    not_found = guard.reconcile("issue_refund", "refund:T-1", verify=lambda ctx: "NOT_FOUND")
    assert not_found is not None and (not_found.state, not_found.resolution) == ("UNKNOWN", "stale")
    calls: list[str] = []
    assert guard.run("issue_refund", REQUEST, ORDER, lambda ctx: calls.append(ctx.idempotency_key),
                     action_key="refund:T-1").previous_outcome == "UNKNOWN"  # fmt: skip
    guard.resolve("issue_refund", "refund:T-1", "FAILED", original_stopped=True)  # a person checked
    assert guard.run("issue_refund", REQUEST, ORDER, lambda ctx: calls.append(ctx.idempotency_key),
                     action_key="refund:T-1").outcome == "SUCCEEDED"  # fmt: skip
    assert calls == [IDEM.key("issue_refund", "refund:T-1")]  # the same idempotency key


# --------------------------------------------------------------------------- the store secret


OTHER_SECRET = bytes(range(100, 132))


def test_a_missing_or_wrong_secret_fails_before_anything_runs(tmp_path: Path) -> None:
    path = tmp_path / "actions.db"
    guard = Guard(POLICY, store=sqlite(path))
    guard.run("issue_refund", REQUEST, ORDER, lambda ctx: "done", action_key="refund:T-1")
    with pytest.raises(TypeError):
        safety.SqliteActionStore(path)  # type: ignore[call-arg] # no secret: no store
    attempts: list[tuple[Any, dict[str, Any], str]] = [
        (None, {}, "at least 32 bytes"),
        (b"short", {}, "at least 32 bytes"),
        (SECRET.hex(), {}, "at least 32 bytes"),  # text is not accepted: no silent re-encoding
        (OTHER_SECRET, {}, "secret does not match"),
        (SECRET, {"key_id": "rotated"}, "uses key id 'default'"),
    ]
    for secret, options, message in attempts:
        with pytest.raises(safety.StoreConfigurationError, match=message) as error:
            safety.SqliteActionStore(path, secret=secret, **options)
        assert SECRET.hex() not in str(error.value) and OTHER_SECRET.hex() not in str(error.value)
    store = sqlite(path)
    assert SECRET.hex() not in repr(store) and str(SECRET) not in repr(store)
    with pytest.raises(TypeError):
        pickle.dumps(store)


def test_a_store_file_replaced_while_open_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "actions.db"
    guard = Guard(POLICY, store=sqlite(path))
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    safety.SqliteActionStore(path, secret=OTHER_SECRET)  # someone recreates it with another secret
    calls: list[str] = []
    result = guard.run("issue_refund", REQUEST, ORDER, lambda ctx: calls.append("x"), action_key="refund:T-1")
    assert (result.decision.reason_codes, result.store_error, calls) == (
        ("store_unavailable",), "StoreConfigurationError", [],
    )  # fmt: skip


def test_the_same_secret_works_across_process_restarts(tmp_path: Path) -> None:
    (child,) = run_children(tmp_path, [("slow", "refund:T-1")])  # a process with the secret from its environment
    assert child.returncode == 0 and json.loads(child.stdout)["outcome"] == "SUCCEEDED"
    calls: list[str] = []
    restarted = Guard(POLICY, store=sqlite(tmp_path / "actions.db"))
    again = restarted.run("issue_refund", REQUEST, ORDER, lambda ctx: calls.append("x"), action_key="refund:T-1")
    assert (again.previous_outcome, calls) == ("SUCCEEDED", [])
    assert len(ledger(tmp_path / "ledger.db")) == 1


def test_stored_digests_resist_dictionary_attacks_without_the_secret(tmp_path: Path) -> None:
    order, amount, ticket = "48213", 87.45, "T-20931"
    policy = safety.parse_policy({"version": 1, "actions": {"issue_refund": {**RULE, "max_amount": "200.00"}}})
    guard = Guard(policy, store=sqlite(tmp_path / "actions.db"))
    request = {"order_id": order, "amount": amount}
    guard.run("issue_refund", request, {"order_id": order, "amount": 120.0}, lambda ctx: "done",
              action_key=f"refund:{ticket}")  # fmt: skip
    with closing(sqlite3.connect(tmp_path / "actions.db")) as db, db:
        (key, request_digest, once_key) = db.execute(
            "SELECT key, request_digest, once_key FROM aobs_safety_actions"
        ).fetchone()
    stored = {key, request_digest, once_key}

    def plain(domain: str, *parts: str) -> str:  # what Stage 1 stored: an unkeyed SHA-256
        h = hashlib.sha256(domain.encode())
        for part in parts:
            h.update(b"\x00" + part.encode())
        return h.hexdigest()

    def keyed(secret: bytes) -> Callable[..., str]:
        def digest(kind: str, *parts: str) -> str:
            h = hmac.new(secret, f"aobs.safety.{kind}.v2".encode(), hashlib.sha256)
            for part in parts:
                h.update(b"\x00" + part.encode())
            return h.hexdigest()

        return digest

    orders = [str(n) for n in range(40_000, 60_000)]
    amounts = [c / 100 for c in range(1, 20_001)]
    stable = safety._stable  # the canonical form the guard hashes (public source)
    candidates = {
        "plain": lambda kind, *parts: plain(f"aobs.safety.{kind}.v1", *parts),
        "plain-v2": lambda kind, *parts: plain(f"aobs.safety.{kind}.v2", *parts),
        "wrong secret": keyed(OTHER_SECRET),
        "right secret": keyed(SECRET),
    }
    found = {}
    for name, digest in candidates.items():
        hits = [o for o in orders if digest("once", "issue_refund", stable((o,))) in stored]
        hits += [
            a for a in amounts if digest("request", "issue_refund", stable({"order_id": order, "amount": a})) in stored
        ]
        hits += [
            t
            for t in (f"T-{n}" for n in range(20_000, 22_000))
            if digest("key", "issue_refund", f"refund:{t}") in stored
        ]
        found[name] = hits
    assert found["plain"] == found["plain-v2"] == found["wrong secret"] == []
    assert found["right secret"] == [order, amount, ticket]  # the search space did contain the answers
    raw = b"".join(f.read_bytes() for f in tmp_path.iterdir() if f.name.startswith("actions.db"))
    for value in (order, ticket, str(amount), IDEM.key("issue_refund", f"refund:{ticket}")):
        assert value.encode() not in raw


def test_provider_idempotency_keys_do_not_depend_on_the_store_secret(tmp_path: Path) -> None:
    keys = []
    for n, secret in enumerate((SECRET, OTHER_SECRET)):
        store = safety.SqliteActionStore(tmp_path / f"{n}.db", secret=secret)
        guard = Guard(POLICY, store=store)
        guard.run("issue_refund", REQUEST, ORDER, lambda ctx: keys.append(ctx.idempotency_key), action_key="refund:T-1")
    assert keys == [IDEM.key("issue_refund", "refund:T-1")] * 2
    assert sqlite(tmp_path / "0.db").digest("key", "issue_refund", "refund:T-1") != safety.SqliteActionStore(
        tmp_path / "1.db", secret=OTHER_SECRET
    ).digest("key", "issue_refund", "refund:T-1")  # while the stored digests do


def test_old_and_unrecognised_store_files_are_refused(tmp_path: Path) -> None:
    def make(name: str, version: int) -> Path:
        path = tmp_path / name
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE aobs_safety_actions (action TEXT, key TEXT)")
            db.execute(f"PRAGMA user_version = {version}")
        path.chmod(0o600)  # (a world-readable file would also be reported: tested separately)
        return path

    for version, reason in ((1, "unkeyed digests"), (2, "no idempotency configuration ids")):
        old = make(f"v{version}.db", version)
        with pytest.raises(safety.StoreConfigurationError, match=rf"schema {version} \({reason}\) cannot be migrated"):
            sqlite(old)
        with closing(sqlite3.connect(old)) as db, db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == version  # left exactly as it was
    with pytest.raises(safety.StoreConfigurationError, match="unrecognised action store"):
        sqlite(make("v0.db", 0))


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_store_files_are_private_and_deleted_records_are_overwritten(tmp_path: Path) -> None:
    previous = os.umask(0)  # even with a permissive umask
    try:
        store = sqlite(tmp_path / "actions.db")
        guard = Guard(POLICY, store=store)
        assert guard.run("issue_refund", REQUEST, ORDER, lambda ctx: "done", action_key="refund:T-1").executed
        guard.run("issue_refund", {"order_id": "2077", "amount": 10.0}, {"order_id": "2077", "amount": 10.0},
                  lambda ctx: "done", action_key="refund:T-2")  # fmt: skip
        with closing(store._connect()) as db:
            assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
            db.execute("SELECT 1")  # while a connection is open the -wal and -shm files exist
            files = sorted(f.name for f in tmp_path.iterdir())
            assert {"actions.db", "actions.db-wal", "actions.db-shm"} <= set(files)
            for name in files:
                assert stat.S_IMODE((tmp_path / name).stat().st_mode) & 0o077 == 0, name
    finally:
        os.umask(previous)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_a_wal_file_removed_while_the_store_opens_is_not_an_error(tmp_path: Path, monkeypatch: Any) -> None:
    sqlite(tmp_path / "actions.db")
    with closing(sqlite3.connect(tmp_path / "actions.db")) as db:
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()  # -wal and -shm exist while it is open
        assert (tmp_path / "actions.db-wal").exists() and (tmp_path / "actions.db-shm").exists()
        real_lstat = Path.lstat

        def lstat(self: Path) -> os.stat_result:
            # another connection closing at this moment: SQLite has just removed the file
            if self.name.endswith(("-wal", "-shm")):
                raise FileNotFoundError(self)
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", lstat)
        assert sqlite(tmp_path / "actions.db").private is True


def test_a_released_reservation_leaves_no_digest_in_the_files(tmp_path: Path) -> None:
    policy = safety.parse_policy({"version": 1, "actions": {"issue_refund": {**RULE, "approval_above": "10.00"}}})
    store = sqlite(tmp_path / "actions.db")
    guard = Guard(policy, store=store)
    guard.run("issue_refund", REQUEST, ORDER, lambda ctx: "done", action_key="refund:T-1", approver=lambda ctx: False)
    assert guard.inspect("issue_refund", "refund:T-1") is None
    released = store.digest("key", "issue_refund", "refund:T-1").encode()
    assert not any(released in f.read_bytes() for f in tmp_path.iterdir())


# --------------------------------------------------------------------------- one once_per rule per store


@pytest.mark.parametrize("kind", sorted(STORES))
@pytest.mark.parametrize("second_rule", [[], ["order_id", "case_id"]])
def test_guards_with_different_once_per_rules_cannot_share_a_store(
    kind: str, tmp_path: Path, second_rule: list[str]
) -> None:
    store = STORES[kind](tmp_path)
    strict = Guard(POLICY, store=store)
    other_rule = {**RULE, "required_evidence": ["order_id", "amount", "case_id"],
                  "must_match": ["order_id", "case_id"], "once_per": second_rule}  # fmt: skip
    other = Guard(safety.parse_policy({"version": 1, "actions": {"issue_refund": other_rule}}), store=store)
    calls: list[str] = []
    assert strict.run(
        "issue_refund", REQUEST, ORDER, lambda c: calls.append("strict"), action_key="refund:T-1"
    ).executed
    result = other.run("issue_refund", {**REQUEST, "case_id": "C-1"}, {**ORDER, "case_id": "C-1"},
                       lambda c: calls.append("other"), action_key="refund:T-2")  # fmt: skip
    assert (result.outcome, result.decision.reason_codes, result.decision.rule) == (
        "NOT_EXECUTED", ("policy_mismatch",), "guard.store",
    )  # fmt: skip
    assert calls == ["strict"]


def test_the_once_per_rule_is_kept_across_restarts(tmp_path: Path) -> None:
    Guard(POLICY, store=sqlite(tmp_path / "a.db")).run(
        "issue_refund", REQUEST, ORDER, lambda c: "done", action_key="refund:T-1"
    )
    loose_rule = {k: v for k, v in RULE.items() if k != "once_per"}
    loose = safety.parse_policy({"version": 1, "actions": {"issue_refund": loose_rule}})
    restarted = Guard(loose, store=sqlite(tmp_path / "a.db"))
    result = restarted.run("issue_refund", REQUEST, ORDER, lambda c: "done", action_key="refund:T-2")
    assert result.decision.reason_codes == ("policy_mismatch",) and not result.executed


def test_secure_delete_is_set_even_where_sqlite_defaults_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aobs import _safety_store

    connect = sqlite3.connect

    def connect_with_secure_delete_off(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.execute("PRAGMA secure_delete=OFF")  # as on SQLite builds without SQLITE_SECURE_DELETE
        return db

    monkeypatch.setattr(_safety_store.sqlite3, "connect", connect_with_secure_delete_off)
    store = sqlite(tmp_path / "actions.db")
    with closing(store._connect()) as db:
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
