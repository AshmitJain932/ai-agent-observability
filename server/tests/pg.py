"""Real PostgreSQL 16 for tests.

Two modes, chosen by :func:`start_from_env`:

* ``AOBS_TEST_DATABASE_URL`` set: use that server. Only databases named ``aobs_test_<random>``
  are created there, and only those are dropped. No other database is read or written.
* Otherwise: a **throwaway cluster** made with ``initdb`` in a fresh ``aobs-pg-*`` directory under
  the system temp directory. It listens on a Unix socket inside that directory only
  (``listen_addresses = ''``), so it never binds a TCP port and cannot clash with an installed
  instance. It is stopped and deleted at the end of the session (and at interpreter exit). An
  installed cluster (e.g. ``/var/lib/postgresql/16/main``), its configuration and its service are
  never used.

PostgreSQL binaries are found via ``AOBS_PG_BIN`` (if set, it is the only place searched), else
``pg_config --bindir``, ``initdb`` on ``PATH``, then the usual install locations. Only major
version 16 is accepted. ``initdb`` refuses to run as root, so as root the cluster tools run as
the ``postgres`` user (override with ``AOBS_PG_RUN_AS``) through ``runuser``.

If no server can be provided, :class:`PgUnavailable` explains the options; the fixtures turn it
into a test **error**, never a silent skip.
"""

from __future__ import annotations

import atexit
import contextlib
import glob
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

REQUIRED_MAJOR = 16
DB_PREFIX = "aobs_test_"
CLUSTER_DIR_PREFIX = "aobs-pg-"
MARKER_FILE = "aobs-test-cluster.json"
SOCKET_PORT = 54316  # only names the socket file; no TCP listener is opened
SUPERUSER = "aobs"
TOOLS = ("initdb", "pg_ctl", "postgres")
DEFAULT_CANDIDATES = (
    f"/usr/lib/postgresql/{REQUIRED_MAJOR}/bin",  # Debian / Ubuntu
    f"/usr/pgsql-{REQUIRED_MAJOR}/bin",  # RHEL / Fedora (PGDG)
    f"/opt/homebrew/opt/postgresql@{REQUIRED_MAJOR}/bin",  # Homebrew, Apple silicon
    f"/usr/local/opt/postgresql@{REQUIRED_MAJOR}/bin",  # Homebrew, Intel
    f"/Applications/Postgres.app/Contents/Versions/{REQUIRED_MAJOR}/bin",
)
_DB_NAME = re.compile(r"^aobs_test_[a-z0-9_]{1,40}$")

HELP = (
    "PostgreSQL 16 is required for tests marked 'pg'. Either set AOBS_TEST_DATABASE_URL to a "
    "PostgreSQL 16 server where the user may CREATE DATABASE (only aobs_test_* databases are "
    "created and dropped), or install PostgreSQL 16 server binaries (initdb, pg_ctl, postgres) "
    "and, if they are not found automatically, set AOBS_PG_BIN to their directory. "
    "To run everything else: pytest -m 'not pg'."
)


class PgUnavailable(RuntimeError):
    """No PostgreSQL 16 server can be provided for the tests."""


# --------------------------------------------------------------------------- binary discovery


def parse_major(version_output: str) -> int:
    """``postgres (PostgreSQL) 16.15 (Ubuntu ...)`` -> 16."""
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", version_output)
    if not match:
        raise ValueError(f"unrecognized postgres --version output: {version_output.strip()!r}")
    return int(match.group(1))


def _check_bindir(bindir: Path) -> str | None:
    """Return None if ``bindir`` holds usable PostgreSQL 16 tools, else the reason it does not."""
    missing = [t for t in TOOLS if not os.access(bindir / t, os.X_OK)]
    if missing:
        return f"{bindir}: missing {', '.join(missing)}"
    try:
        out = subprocess.run(
            [str(bindir / "postgres"), "--version"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
        major = parse_major(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"{bindir}: cannot determine version ({exc})"
    if major != REQUIRED_MAJOR:
        return f"{bindir}: PostgreSQL {major}, but {REQUIRED_MAJOR} is required"
    return None


def find_bindir(env: Mapping[str, str] | None = None, candidates: Sequence[str] = DEFAULT_CANDIDATES) -> Path:
    environ = os.environ if env is None else env
    explicit = environ.get("AOBS_PG_BIN")
    if explicit:
        reason = _check_bindir(Path(explicit))
        if reason:
            raise PgUnavailable(f"AOBS_PG_BIN is set but unusable: {reason}. {HELP}")
        return Path(explicit)

    path = environ.get("PATH", "")
    found: list[Path] = []
    pg_config = shutil.which("pg_config", path=path)
    if pg_config:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True, timeout=30, check=True)
            found.append(Path(out.stdout.strip()))
    initdb = shutil.which("initdb", path=path)
    if initdb:
        found.append(Path(initdb).resolve().parent)
    for pattern in candidates:
        found.extend(Path(p) for p in sorted(glob.glob(pattern)))

    reasons: list[str] = []
    for bindir in dict.fromkeys(found):  # de-duplicate, keep order
        reason = _check_bindir(bindir)
        if reason is None:
            return bindir
        reasons.append(reason)
    tried = "; ".join(reasons) if reasons else "no PostgreSQL binaries found on PATH or in the usual locations"
    raise PgUnavailable(f"No usable PostgreSQL {REQUIRED_MAJOR} binaries ({tried}). {HELP}")


def _run_as_user(environ: Mapping[str, str]) -> str | None:
    """The user to run cluster tools as, or None to run them as the current user."""
    configured = environ.get("AOBS_PG_RUN_AS")
    user = configured or ("postgres" if os.geteuid() == 0 else None)
    if user is None:
        return None
    try:
        pwd.getpwnam(user)
    except KeyError:
        raise PgUnavailable(
            f"initdb cannot run as root and user {user!r} does not exist; set AOBS_PG_RUN_AS to an "
            f"unprivileged user. {HELP}"
        ) from None
    if shutil.which("runuser") is None:
        raise PgUnavailable(f"running as root needs 'runuser' to start PostgreSQL as {user!r}. {HELP}")
    return user


# --------------------------------------------------------------------------- servers


class PgServer:
    """A PostgreSQL server the tests may create (and drop) their own databases on."""

    mode: str

    def __init__(self, admin_url: URL) -> None:
        self._admin_url = admin_url  # libpq-style URL ("postgresql://...") of a maintenance database
        self._created: list[str] = []
        self.template: str | None = None

    # URLs ----------------------------------------------------------------
    def libpq_url(self, database: str) -> str:
        return self._admin_url.set(drivername="postgresql", database=database).render_as_string(hide_password=False)

    def sqlalchemy_url(self, database: str) -> URL:
        return self._admin_url.set(drivername="postgresql+psycopg", database=database)

    def admin_connect(self) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(
            self._admin_url.render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://"),
            autocommit=True,
        )

    # databases -----------------------------------------------------------
    def server_major(self) -> int:
        with self.admin_connect() as conn:
            row = conn.execute("SHOW server_version_num").fetchone()
        assert row is not None
        return int(str(row[0])) // 10000

    def create_database(self, template: str | None = None) -> str:
        name = f"{DB_PREFIX}{secrets.token_hex(6)}"
        query = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(template or "template0")
        )
        with self.admin_connect() as conn:
            conn.execute(query)
        self._created.append(name)
        return name

    def drop_database(self, name: str) -> None:
        if name not in self._created or not _DB_NAME.match(name):
            raise ValueError(f"refusing to drop database {name!r}: not created by this test session")
        with self.admin_connect() as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        self._created.remove(name)

    def close(self) -> None:
        for name in list(reversed(self._created)):
            with contextlib.suppress(psycopg.Error):
                self.drop_database(name)


class ExternalServer(PgServer):
    mode = "external"


class ThrowawayCluster(PgServer):
    """A private cluster in a temp directory; socket-only; deleted on :meth:`close`."""

    mode = "cluster"

    def __init__(self, bindir: Path, run_as: str | None, base_dir: str | None = None) -> None:
        self.bindir = bindir
        self.run_as = run_as
        self.root = Path(tempfile.mkdtemp(prefix=CLUSTER_DIR_PREFIX, dir=base_dir))
        self.data_dir = self.root / "data"
        self.socket_dir = self.root / "sock"
        self.log_file = self.root / "server.log"
        self._started = False
        (self.root / MARKER_FILE).write_text(json.dumps({"pid": os.getpid()}))
        self.socket_dir.mkdir(mode=0o700)
        if run_as:
            for path in (self.root, self.socket_dir):
                shutil.chown(path, user=run_as)
        super().__init__(
            URL.create(
                "postgresql",
                username=SUPERUSER,
                database="postgres",
                query={"host": str(self.socket_dir), "port": str(SOCKET_PORT)},
            )
        )

    def _cmd(self, tool: str, *args: str) -> list[str]:
        cmd = [str(self.bindir / tool), *args]
        return ["runuser", "-u", self.run_as, "--", *cmd] if self.run_as else cmd

    def _run(self, tool: str, *args: str, timeout: float = 120) -> None:
        result = subprocess.run(
            self._cmd(tool, *args), cwd=self.root, capture_output=True, text=True, timeout=timeout, check=False
        )
        if result.returncode != 0:
            log = self.log_file.read_text(errors="replace")[-4000:] if self.log_file.exists() else ""
            raise PgUnavailable(
                f"{tool} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
                + (f"\nserver log:\n{log}" if log else "")
            )

    def start(self) -> None:
        self._run(
            "initdb",
            "-D", str(self.data_dir),
            "-U", SUPERUSER,
            "-E", "UTF8",
            "--locale=C",
            "--auth-local=trust",
            "--auth-host=reject",
            "--no-instructions",
        )  # fmt: skip
        with (self.data_dir / "postgresql.conf").open("a") as conf:
            conf.write(
                "\n# aobs test cluster: Unix socket in a private directory only, no TCP listener\n"
                "listen_addresses = ''\n"
                f"unix_socket_directories = '{self.socket_dir}'\n"
                f"port = {SOCKET_PORT}\n"
                "fsync = on\n"
            )
        self._run("pg_ctl", "-D", str(self.data_dir), "-l", str(self.log_file), "-w", "-t", "60", "start")
        self._started = True

    def stop(self, mode: str = "fast") -> None:
        if self._started:
            self._run("pg_ctl", "-D", str(self.data_dir), "-m", mode, "-w", "-t", "60", "stop")
            self._started = False

    def close(self) -> None:
        try:
            if self._started:
                super().close()
                self.stop()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


def reap_stale_clusters(bindir: Path | None, run_as: str | None, base_dir: str | None = None) -> list[Path]:
    """Stop and delete throwaway clusters left behind by test sessions that died (e.g. SIGKILL).

    Only directories named ``aobs-pg-*`` that contain this harness's marker file, written by a
    process that no longer exists, are touched.
    """
    reaped: list[Path] = []
    base = Path(base_dir or tempfile.gettempdir())
    for root in sorted(base.glob(f"{CLUSTER_DIR_PREFIX}*")):
        marker = root / MARKER_FILE
        try:
            pid = int(json.loads(marker.read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _pid_alive(pid):
            continue
        if bindir is not None and (root / "data" / "postmaster.pid").exists():
            cmd = [str(bindir / "pg_ctl"), "-D", str(root / "data"), "-m", "immediate", "-w", "-t", "30", "stop"]
            if run_as:
                cmd = ["runuser", "-u", run_as, "--", *cmd]
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(cmd, capture_output=True, timeout=60, check=False, cwd=base)
        shutil.rmtree(root, ignore_errors=True)
        reaped.append(root)
    return reaped


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_from_env(env: Mapping[str, str] | None = None) -> PgServer:
    environ = os.environ if env is None else env
    server: PgServer
    external = environ.get("AOBS_TEST_DATABASE_URL", "").strip()
    if external:
        url = make_url(external)
        server = ExternalServer(url.set(drivername="postgresql"))
    else:
        bindir = find_bindir(environ)
        run_as = _run_as_user(environ)
        reap_stale_clusters(bindir, run_as)
        cluster = ThrowawayCluster(bindir, run_as)
        atexit.register(cluster.close)
        try:
            cluster.start()
        except BaseException:
            cluster.close()
            raise
        server = cluster
    try:
        major = server.server_major()
    except psycopg.Error as exc:
        server.close()
        raise PgUnavailable(f"cannot connect to the test PostgreSQL server: {exc}. {HELP}") from None
    if major != REQUIRED_MAJOR:
        server.close()
        raise PgUnavailable(f"the test server runs PostgreSQL {major}; {REQUIRED_MAJOR} is required. {HELP}")
    # Per-test databases are cloned from this template (empty until migrations exist).
    server.template = server.create_database()
    return server
