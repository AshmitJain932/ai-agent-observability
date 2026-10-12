"""Check or update schema/v1/SCHEMA_LOCK (the schema file's hash and version).

    python -m aobs_server.model.schema_lock            # check, exit 1 on mismatch
    python -m aobs_server.model.schema_lock --write    # record the current hash

Writing the lock is a deliberate act that should accompany a version bump and a CHANGELOG entry;
the tests check all three agree.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = REPO_ROOT / "schema" / "v1" / "ingest.schema.json"
LOCK_PATH = REPO_ROOT / "schema" / "v1" / "SCHEMA_LOCK"


def current() -> dict[str, str]:
    raw = SCHEMA_PATH.read_bytes()
    version = json.loads(raw)["x-schema-version"]
    return {"schema_version": version, "sha256": hashlib.sha256(raw).hexdigest()}


def main(argv: list[str]) -> int:
    now = current()
    if "--write" in argv:
        LOCK_PATH.write_text(json.dumps(now, indent=2) + "\n")
        print(f"wrote {LOCK_PATH.relative_to(REPO_ROOT)}: {now}")
        return 0
    locked = json.loads(LOCK_PATH.read_text())
    if locked != now:
        print(f"schema lock mismatch: locked={locked} current={now}", file=sys.stderr)
        return 1
    print("schema lock OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
