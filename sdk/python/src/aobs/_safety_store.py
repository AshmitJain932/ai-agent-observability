"""Where :class:`aobs.safety.ActionGuard` keeps the state of side-effecting actions.

A store holds one :class:`ActionRecord` per logical action, identified by ``(action, key)``. The
store computes every identifier it persists with a **keyed** HMAC-SHA256 (:meth:`ActionStore.digest`):
the action key, the checked request and the ``once_per`` values. A plain hash would not protect
them: order ids, amounts and ticket numbers have so few possible values that anyone holding the
file could hash every candidate and compare. With an HMAC that requires the secret.

What the file still shows to someone who can read it, secret or not: action names, states, how an
outcome was settled, error types, timestamps (``attempt_id`` is a UUIDv7, which also encodes its
creation time), how many records there are, and which records share a digest (e.g. a failed and a
later refund for the same order). Someone who can *write* the file can change or delete records;
an HMAC does not prevent that. Protect the file with file-system permissions.

This is local coordination: one file, on one host. It decides which attempt may call the external
system and records what happened; it cannot stop a request that a paused process has already
prepared from reaching the provider later. Only the provider (honouring the idempotency key) can.

States: ``RESERVED`` (decided, not executed; a lease bounds how long a crashed holder blocks)
→ ``EXECUTING`` (committed, with its start time, *before* the side effect is attempted) →
``SUCCEEDED`` | ``FAILED`` | ``UNKNOWN``. Every write increments ``version``; the attempt that
executes an action may settle it only if nothing else changed the record since (see
:meth:`aobs.safety.ActionGuard.reconcile`).

To use another database (e.g. the application's PostgreSQL), implement :class:`ActionStore` with
the same atomicity: each method is one transaction that is committed before it returns, and
``reserve`` decides with :func:`reservation_conflict` while holding the records it read locked.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol

State = Literal["RESERVED", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN"]
# none: the executing attempt itself; verified: a read-only lookup; manual: a person;
# manual_stopped: a person, who also stated that the stale attempt's process had stopped (not
# verified by the guard); stale: declared stale by reconcile (its process may still be running);
# conflict: the executing attempt reported success after the record had been settled otherwise.
Resolution = Literal["none", "verified", "manual", "manual_stopped", "stale", "conflict"]
DigestKind = Literal["key", "request", "once", "attempt"]
MIN_SECRET_BYTES = 32
_MIN_DISTINCT_BYTES = 8


def check_secret(secret: Any, what: str, error: type[ValueError]) -> bytes:
    """A copy of ``secret`` if it is at least 32 bytes and not obviously non-random. The check only
    rejects values that cannot be random (e.g. one byte repeated); it cannot prove that a value is.
    Messages never include the value."""
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
        raise error(f"{what} must be at least {MIN_SECRET_BYTES} bytes, from a cryptographic random source")
    if len(set(secret)) < _MIN_DISTINCT_BYTES:
        raise error(f"{what} is not random (fewer than {_MIN_DISTINCT_BYTES} distinct byte values)")
    return bytes(secret)


class UnsafePermissionsWarning(UserWarning):
    """The store's file can be read or written by other users of this host."""


class StoreConfigurationError(ValueError):
    """The store cannot be used safely as configured (wrong secret, old format, mismatched rules).
    Messages never contain secrets or digests."""


class PolicyMismatch(StoreConfigurationError):
    """A guard's ``once_per`` rule for an action differs from the one this store enforces."""


@dataclass(frozen=True)
class ActionRecord:
    """The store's state of one logical action: keyed digests and states, never raw values."""

    action: str
    key: str  # keyed digest of the action key (or of the attempt, without one)
    attempt_id: str  # the attempt (its action_id) that holds the record
    state: State
    request_digest: str  # keyed digest of the checked request: one action key means one request
    once_key: str | None  # keyed digest of the verified ``once_per`` values, if the policy has that rule
    lease_until: float  # while RESERVED: after this (Unix time) an attempt that never executed may be replaced
    resolution: Resolution = "none"
    error_type: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None  # when it became EXECUTING (durable)
    version: int = 0  # incremented by every write: the fencing token for compare-and-set
    idempotency_id: str | None = None  # which idempotency configuration its provider key comes from


class ActionStore(Protocol):
    """Atomic, committed operations on :class:`ActionRecord` s. See the module docstring."""

    kind: str  # recorded in traces, e.g. "memory" or "sqlite"
    durable: bool  # shared by processes and kept across restarts: then every action needs an action_key

    def digest(self, kind: DigestKind, *parts: str) -> str:
        """The keyed digest the store persists for these values (the same in every process)."""
        ...

    def reserve(self, claim: ActionRecord, now: float, *, once_per: tuple[str, ...]) -> ActionRecord | None:
        """Store ``claim`` (state RESERVED) and return None, or return the record that blocks it.
        Raises :class:`PolicyMismatch` if ``once_per`` differs from the rule the store holds for
        the action (the first reservation of an action records it)."""
        ...

    def update(
        self,
        action: str,
        key: str,
        attempt_id: str,
        *,
        expect: tuple[State, ...],
        state: State,
        now: float,
        expect_version: int | None = None,
        resolution: Resolution | None = None,
        error_type: str | None = None,
        started_at: float | None = None,
    ) -> tuple[bool, ActionRecord | None]:
        """Move the record to ``state`` if ``attempt_id`` holds it in one of the ``expect`` states
        (and at ``expect_version``, if given). Returns (whether it moved, the record as it now is)."""
        ...

    def release(self, action: str, key: str, attempt_id: str) -> None:
        """Delete the record if ``attempt_id`` holds it and it is still RESERVED (never executed)."""
        ...

    def get(self, action: str, key: str) -> ActionRecord | None: ...


def _hmac(secret: bytes, kind: str, parts: tuple[str, ...]) -> str:
    h = hmac.new(secret, f"aobs.safety.{kind}.v2".encode(), hashlib.sha256)
    for part in parts:
        h.update(b"\x00" + part.encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def _replaceable(record: ActionRecord, now: float) -> bool:
    """A record a new attempt may take over: it definitely did not happen."""
    return record.state == "FAILED" or (record.state == "RESERVED" and record.lease_until <= now)


def reservation_conflict(
    claim: ActionRecord, same_key: ActionRecord | None, same_once: ActionRecord | None, now: float
) -> tuple[ActionRecord | None, ActionRecord | None]:
    """The reservation rule every store applies inside its transaction.

    ``same_key`` is the record with the claim's (action, key); ``same_once`` a record of *another*
    key holding the claim's ``once_key`` in any state but FAILED. Returns ``(blocking, evict)``:
    the record that blocks the claim (None: store it), and an expired, never-executed reservation
    that must be deleted first.
    """
    if same_key is not None:
        if same_key.request_digest != claim.request_digest:
            return same_key, None  # the same action key with a different request: never
        if same_key.idempotency_id != claim.idempotency_id:
            return same_key, None  # its provider key came from another configuration: never re-key silently
        if not _replaceable(same_key, now):
            return same_key, None  # reserved, executing, succeeded or unknown
    if same_once is not None:
        if not _replaceable(same_once, now):
            return same_once, None
        return None, same_once
    return None, None


def _check_claim(claim: ActionRecord, once_per: tuple[str, ...]) -> None:
    if (claim.once_key is None) != (not once_per):
        raise ValueError("a claim has a once_key exactly when the rule has once_per fields")


def _rule_mismatch(action: str) -> NoReturn:
    raise PolicyMismatch(
        f"the once_per rule for {action!r} differs from the one this store enforces; every guard "
        "sharing a store must use the same rule (use a new store to change it)"
    )


def _moved(
    record: ActionRecord,
    *,
    state: State,
    now: float,
    resolution: Resolution | None,
    error_type: str | None,
    started_at: float | None,
) -> ActionRecord:
    return replace(
        record,
        state=state,
        resolution=resolution or record.resolution,
        error_type=error_type if error_type is not None else record.error_type,
        started_at=started_at if started_at is not None else record.started_at,
        updated_at=now,
        version=record.version + 1,
    )


def _applies(record: ActionRecord | None, attempt_id: str, expect: tuple[State, ...], version: int | None) -> bool:
    return (
        record is not None
        and record.attempt_id == attempt_id
        and record.state in expect
        and (version is None or record.version == version)
    )


class MemoryActionStore:
    """In this process only, and lost on restart: for development, tests and single-process tools.
    Guards share it only if they are given the same store object. Its digests use a random key that
    exists only in this object."""

    kind = "memory"
    durable = False

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ActionRecord] = {}
        self._rules: dict[str, tuple[str, ...]] = {}
        self._lock = threading.Lock()
        self.__secret = secrets.token_bytes(MIN_SECRET_BYTES)

    def __reduce__(self) -> NoReturn:
        raise TypeError("an action store cannot be pickled")

    def digest(self, kind: DigestKind, *parts: str) -> str:
        return _hmac(self.__secret, kind, parts)

    def reserve(self, claim: ActionRecord, now: float, *, once_per: tuple[str, ...]) -> ActionRecord | None:
        _check_claim(claim, once_per)
        with self._lock:
            if self._rules.setdefault(claim.action, once_per) != once_per:
                _rule_mismatch(claim.action)
            same_key = self._records.get((claim.action, claim.key))
            same_once = None
            if claim.once_key is not None:
                same_once = next(
                    (
                        r
                        for r in self._records.values()
                        if r.action == claim.action
                        and r.once_key == claim.once_key
                        and r.key != claim.key
                        and r.state != "FAILED"
                    ),
                    None,
                )
            blocking, evict = reservation_conflict(claim, same_key, same_once, now)
            if blocking is not None:
                return blocking
            if evict is not None:
                del self._records[(evict.action, evict.key)]
            version = same_key.version + 1 if same_key is not None else 1
            self._records[(claim.action, claim.key)] = replace(claim, created_at=now, updated_at=now, version=version)
            return None

    def update(
        self,
        action: str,
        key: str,
        attempt_id: str,
        *,
        expect: tuple[State, ...],
        state: State,
        now: float,
        expect_version: int | None = None,
        resolution: Resolution | None = None,
        error_type: str | None = None,
        started_at: float | None = None,
    ) -> tuple[bool, ActionRecord | None]:
        with self._lock:
            record = self._records.get((action, key))
            if record is None or not _applies(record, attempt_id, expect, expect_version):
                return False, record
            record = _moved(
                record, state=state, now=now, resolution=resolution, error_type=error_type, started_at=started_at
            )
            self._records[(action, key)] = record
            return True, record

    def release(self, action: str, key: str, attempt_id: str) -> None:
        with self._lock:
            record = self._records.get((action, key))
            if record is not None and record.attempt_id == attempt_id and record.state == "RESERVED":
                del self._records[(action, key)]

    def get(self, action: str, key: str) -> ActionRecord | None:
        with self._lock:
            return self._records.get((action, key))


_SCHEMA_VERSION = 3
_OLD_SCHEMAS = {1: "unkeyed digests", 2: "no idempotency configuration ids"}
_TABLE = "aobs_safety_actions"
_META = "aobs_safety_meta"
_RULES = "aobs_safety_rules"
_COLUMNS = tuple(f.name for f in fields(ActionRecord))
_SCHEMA = f"""
CREATE TABLE {_TABLE} (
    action TEXT NOT NULL,
    key TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')),
    request_digest TEXT NOT NULL,
    once_key TEXT,
    lease_until REAL NOT NULL,
    resolution TEXT NOT NULL
        CHECK (resolution IN ('none', 'verified', 'manual', 'manual_stopped', 'stale', 'conflict')),
    error_type TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    version INTEGER NOT NULL,
    idempotency_id TEXT,
    PRIMARY KEY (action, key)
);
CREATE UNIQUE INDEX {_TABLE}_once ON {_TABLE} (action, once_key)
    WHERE once_key IS NOT NULL AND state != 'FAILED';
CREATE TABLE {_META} (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE {_RULES} (action TEXT PRIMARY KEY, once_per TEXT NOT NULL)
"""


class SqliteActionStore:
    """Durable on one host: survives restarts, and coordinates every thread and process that opens
    the same file through SQLite's file locks (``BEGIN IMMEDIATE`` transactions, WAL mode,
    ``synchronous=FULL``).

    Assumptions: a local file system whose locks and shared memory work (not NFS, SMB or other
    network file systems: SQLite's locking is unreliable there), and storage that honours fsync.
    Opening fails if WAL mode cannot be enabled. For several hosts, implement :class:`ActionStore`
    on a shared database.

    ``secret`` (at least 32 bytes from a cryptographic random source, e.g. ``secrets.token_bytes(32)``
    stored in a secret manager) keys every digest in the file. Values that cannot be random (one
    byte repeated, few distinct bytes) are refused, but no check can prove a value is random.
    Every process that shares the file must use the same secret, across restarts. The file records
    ``key_id`` and a check value: opening it with a missing or different secret raises
    :class:`StoreConfigurationError` before anything can run, because a different secret would make
    existing records invisible and allow duplicates. There is no in-place rotation (the original
    values are not stored): to change the secret, settle the open actions and start a new file.

    The path must not be a symbolic link. A new file is created with mode 0600 (POSIX), and SQLite
    gives its ``-wal`` and ``-shm`` files the database's mode. An existing file keeps its mode: if
    other users can read or write it (or its ``-wal``/``-shm`` files), an
    :class:`UnsafePermissionsWarning` is issued and ``private`` is False; it is not changed and not
    treated as protected. ``private`` is None where POSIX modes do not apply. Deleted records are
    overwritten (``secure_delete``).
    """

    kind = "sqlite"
    durable = True

    def __init__(self, path: str | Path, *, secret: bytes, key_id: str = "default", timeout_s: float = 10.0) -> None:
        self.__secret = check_secret(secret, "secret", StoreConfigurationError)
        if not isinstance(key_id, str) or not key_id or len(key_id) > 64 or not key_id.isprintable():
            raise StoreConfigurationError("key_id must be 1-64 printable characters")
        self.path = Path(path)
        self.key_id = key_id
        self._timeout_s = timeout_s
        self.__check = _hmac(self.__secret, "store-check", (key_id,))
        self.private = self._open_file()
        mode = self._enable_wal()
        if mode.lower() != "wal":
            raise StoreConfigurationError(
                f"{self.path}: SQLite could not enable WAL mode (journal_mode={mode}); the store needs a local "
                "file system with working locks and shared memory"
            )
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                ours = db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name LIKE 'aobs\\_safety\\_%' ESCAPE '\\'"
                ).fetchone()[0]
                if ours:
                    raise StoreConfigurationError(f"{self.path}: unrecognised action store (no schema version)")
                for statement in _SCHEMA.split(";"):
                    db.execute(statement)
                db.executemany(f"INSERT INTO {_META} VALUES (?, ?)", [("key_id", key_id), ("key_check", self.__check)])
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif version in _OLD_SCHEMAS:
                raise StoreConfigurationError(
                    f"{self.path}: action store schema {version} ({_OLD_SCHEMAS[version]}) cannot be migrated: its "
                    "records cannot be converted without their original values. Settle its open actions with the "
                    "version that created it, then use a new file."
                )
            elif version != _SCHEMA_VERSION:
                raise StoreConfigurationError(
                    f"{self.path}: action store schema version {version}, expected {_SCHEMA_VERSION}"
                )
            else:
                self._verify_key(db)

    def __repr__(self) -> str:
        return f"SqliteActionStore({str(self.path)!r}, key_id={self.key_id!r})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("an action store cannot be pickled")

    def _open_file(self) -> bool | None:
        """Create the file with mode 0600 (no effect if it exists), refuse symbolic links and
        anything but a regular file, and report whether the files are private to this user."""
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        with contextlib.suppress(FileExistsError):
            os.close(os.open(self.path, flags, 0o600))
        info = os.lstat(self.path)
        if stat.S_ISLNK(info.st_mode):
            raise StoreConfigurationError(f"{self.path}: is a symbolic link; give the store the real path")
        if not stat.S_ISREG(info.st_mode):
            raise StoreConfigurationError(f"{self.path}: is not a regular file")
        if os.name != "posix":
            return None
        shared = []
        for f in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                mode = f.lstat().st_mode
            except FileNotFoundError:
                # SQLite removes -wal and -shm when another connection to the store closes, which
                # can happen between any check and this call: a missing file shares nothing.
                continue
            if stat.S_IMODE(mode) & 0o077:
                shared.append(f.name)
        if shared:
            warnings.warn(
                f"{self.path}: {', '.join(shared)} can be read or written by other users; the action store is not "
                "private (restrict it, e.g. chmod 600)",
                UnsafePermissionsWarning,
                stacklevel=3,
            )
            return False
        return True

    def _enable_wal(self) -> str:
        """Switch to WAL; returns the journal mode SQLite reports. SQLite's busy timeout does not
        cover this switch, so another process opening the same new file at the same moment can make
        it fail with "database is locked": retried until ``timeout_s``."""
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                with closing(self._connect()) as db:
                    return str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    def _verify_key(self, db: sqlite3.Connection) -> None:
        meta = dict(db.execute(f"SELECT name, value FROM {_META}").fetchall())
        if meta.get("key_id") != self.key_id:
            raise StoreConfigurationError(
                f"{self.path}: this store uses key id {meta.get('key_id')!r}, not {self.key_id!r}"
            )
        if not hmac.compare_digest(str(meta.get("key_check", "")), self.__check):
            raise StoreConfigurationError(f"{self.path}: the secret does not match this store (key id {self.key_id!r})")

    def digest(self, kind: DigestKind, *parts: str) -> str:
        return _hmac(self.__secret, kind, parts)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self._timeout_s, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA secure_delete=ON")
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")  # takes the write lock now: read-decide-write is atomic
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    @staticmethod
    def _record(row: Any) -> ActionRecord | None:
        return None if row is None else ActionRecord(**{name: row[name] for name in _COLUMNS})

    def _get(self, db: sqlite3.Connection, action: str, key: str) -> ActionRecord | None:
        return self._record(
            db.execute(f"SELECT * FROM {_TABLE} WHERE action = ? AND key = ?", (action, key)).fetchone()
        )

    def reserve(self, claim: ActionRecord, now: float, *, once_per: tuple[str, ...]) -> ActionRecord | None:
        _check_claim(claim, once_per)
        with self._transaction() as db:
            self._verify_key(db)  # the file could have been replaced since it was opened
            rule = json.dumps(list(once_per))
            stored = db.execute(f"SELECT once_per FROM {_RULES} WHERE action = ?", (claim.action,)).fetchone()
            if stored is None:
                db.execute(f"INSERT INTO {_RULES} VALUES (?, ?)", (claim.action, rule))
            elif stored[0] != rule:
                _rule_mismatch(claim.action)
            same_key = self._get(db, claim.action, claim.key)
            same_once = None
            if claim.once_key is not None:
                same_once = self._record(
                    db.execute(
                        f"SELECT * FROM {_TABLE} WHERE action = ? AND once_key = ? AND key != ? AND state != 'FAILED'",
                        (claim.action, claim.once_key, claim.key),
                    ).fetchone()
                )
            blocking, evict = reservation_conflict(claim, same_key, same_once, now)
            if blocking is not None:
                return blocking
            if evict is not None:
                db.execute(
                    f"DELETE FROM {_TABLE} WHERE action = ? AND key = ? AND attempt_id = ? AND state = 'RESERVED'",
                    (evict.action, evict.key, evict.attempt_id),
                )
            version = same_key.version + 1 if same_key is not None else 1
            record = replace(claim, created_at=now, updated_at=now, version=version)
            db.execute(
                f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)}) "
                "ON CONFLICT (action, key) DO UPDATE SET attempt_id = excluded.attempt_id, state = excluded.state, "
                "request_digest = excluded.request_digest, once_key = excluded.once_key, "
                "lease_until = excluded.lease_until, resolution = excluded.resolution, "
                "error_type = excluded.error_type, updated_at = excluded.updated_at, "
                "started_at = excluded.started_at, version = excluded.version",
                tuple(getattr(record, name) for name in _COLUMNS),
            )
            return None

    def update(
        self,
        action: str,
        key: str,
        attempt_id: str,
        *,
        expect: tuple[State, ...],
        state: State,
        now: float,
        expect_version: int | None = None,
        resolution: Resolution | None = None,
        error_type: str | None = None,
        started_at: float | None = None,
    ) -> tuple[bool, ActionRecord | None]:
        with self._transaction() as db:
            record = self._get(db, action, key)
            if record is None or not _applies(record, attempt_id, expect, expect_version):
                return False, record
            record = _moved(
                record, state=state, now=now, resolution=resolution, error_type=error_type, started_at=started_at
            )
            db.execute(
                f"UPDATE {_TABLE} SET state = ?, resolution = ?, error_type = ?, updated_at = ?, started_at = ?, "
                "version = ? WHERE action = ? AND key = ?",
                (
                    record.state,
                    record.resolution,
                    record.error_type,
                    now,
                    record.started_at,
                    record.version,
                    action,
                    key,
                ),
            )
            return True, record

    def release(self, action: str, key: str, attempt_id: str) -> None:
        with self._transaction() as db:
            db.execute(
                f"DELETE FROM {_TABLE} WHERE action = ? AND key = ? AND attempt_id = ? AND state = 'RESERVED'",
                (action, key, attempt_id),
            )

    def get(self, action: str, key: str) -> ActionRecord | None:
        with closing(self._connect()) as db:
            return self._get(db, action, key)
