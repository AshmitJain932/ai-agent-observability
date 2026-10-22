"""The SDK imports only the standard library, even though the workspace environment now also
contains the server's dependencies (FastAPI, SQLAlchemy, ...)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import aobs

PROBE = """
import json, sys, sysconfig
import aobs, aobs.export, aobs.integrations.openai_compat, aobs.integrations.anthropic, aobs.privacy, aobs.fingerprint
stdlib = {sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]}
mods = {}
for name, mod in list(sys.modules.items()):
    path = getattr(mod, "__file__", None)
    if path and not any(path.startswith(p) for p in stdlib):
        mods[name] = path
print(json.dumps(mods))
"""


def test_sdk_imports_only_stdlib() -> None:
    out = subprocess.run([sys.executable, "-I", "-c", PROBE], capture_output=True, text=True, check=True, timeout=60)
    package_dir = str(Path(aobs.__file__).resolve().parent)
    outside = {
        name: path
        for name, path in json.loads(out.stdout).items()
        if not str(Path(path).resolve()).startswith(package_dir)
    }
    assert outside == {}, f"aobs imported non-stdlib modules: {sorted(outside)}"
