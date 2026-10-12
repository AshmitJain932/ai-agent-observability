"""Keyed input fingerprints: correct format, stable within a key, and the key never leaves."""

from __future__ import annotations

import base64
import os
import pickle
import re
from collections.abc import Callable

import pytest

import aobs
from aobs.config import ConfigError
from aobs.export import InMemoryExporter
from aobs.fingerprint import Fingerprinter
from sdk_support import end_by_name, ends, flush

PATTERN = re.compile(r"^hmac-sha256:[a-z0-9_-]{1,32}:[0-9a-f]{64}$")
KEY = bytes(range(32))


def _all_forms(key: bytes) -> list[bytes]:
    return [
        key,
        key.hex().encode(),
        key.hex().upper().encode(),
        base64.b64encode(key),
        base64.urlsafe_b64encode(key),
        base64.b64encode(key).rstrip(b"="),
    ]


def test_format_and_determinism() -> None:
    fp = Fingerprinter(KEY, "k1")
    a = fp.fingerprint({"order_id": "1042", "amount": 5})
    assert PATTERN.match(a)
    assert a == fp.fingerprint({"amount": 5, "order_id": "1042"})  # canonical: key order irrelevant
    assert a != fp.fingerprint({"order_id": "1043", "amount": 5})
    assert a != Fingerprinter(bytes(range(1, 33)), "k1").fingerprint({"order_id": "1042", "amount": 5})


def test_same_tool_input_same_fingerprint_within_process(sdk: InMemoryExporter) -> None:
    @aobs.tool
    def lookup(order_id: str, verbose: bool = False) -> None:
        pass

    lookup("1042")
    lookup(order_id="1042", verbose=False)  # defaults applied, keyword vs positional irrelevant
    lookup("1043")
    flush()
    fps = [e["input_fingerprint"] for e in ends(sdk)]
    assert fps[0] == fps[1] != fps[2]


def test_method_receiver_is_excluded(sdk: InMemoryExporter) -> None:
    class Tools:
        @aobs.tool
        def search(self, query: str) -> None:
            pass

    Tools().search("x")
    Tools().search("x")  # different instance, same input
    flush()
    first, second = (e["input_fingerprint"] for e in ends(sdk))
    assert first == second


def test_key_never_appears_in_any_payload(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(fingerprint_key=KEY, fingerprint_key_id="team-key-1", capture="full")

    @aobs.tool
    def lookup(order_id: str) -> str:
        return order_id

    with aobs.run("answer"):
        lookup("1042")
    flush()
    wire = b"".join(sdk.bodies)
    for form in _all_forms(KEY):
        assert form not in wire
    assert end_by_name(sdk, "lookup")["input_fingerprint"].startswith("hmac-sha256:team-key-1:")
    snapshot = str(aobs.diagnostics()).encode()
    tracer = aobs.get_tracer()
    assert tracer is not None
    for text in (snapshot, repr(tracer.config).encode(), repr(tracer.fingerprinter).encode()):
        for form in _all_forms(KEY):
            assert form not in text


def test_fingerprinter_cannot_be_pickled() -> None:
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(Fingerprinter(KEY, "k1"))


def test_configured_key_from_environment_is_stable_across_inits(
    make_sdk: Callable[..., InMemoryExporter], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AOBS_FINGERPRINT_KEY", KEY.hex())
    monkeypatch.setenv("AOBS_FINGERPRINT_KEY_ID", "env-key")
    results = []
    for _ in range(2):
        sdk = make_sdk()
        with aobs.tool_span("t") as sp:
            sp.fingerprint_input({"q": "same"})
        flush()
        results.append(end_by_name(sdk, "t")["input_fingerprint"])
        aobs.shutdown()
    assert results[0] == results[1] and results[0].startswith("hmac-sha256:env-key:")


def test_default_key_is_random_per_process_configuration(make_sdk: Callable[..., InMemoryExporter]) -> None:
    ids = set()
    for _ in range(2):
        make_sdk()
        tracer = aobs.get_tracer()
        assert tracer is not None and tracer.fingerprinter is not None
        ids.add(tracer.fingerprinter.key_id)
        aobs.shutdown()
    assert len(ids) == 2  # a fresh random key (and id) each time; never a fixed built-in key


@pytest.mark.parametrize(
    ("key", "key_id"),
    [(b"short", "k1"), (KEY, None), (KEY, "Bad Id!")],
)
def test_weak_or_incomplete_key_config(
    make_sdk: Callable[..., InMemoryExporter], key: bytes, key_id: str | None
) -> None:
    with pytest.raises(ConfigError):
        make_sdk(fingerprint_key=key, fingerprint_key_id=key_id, strict=True)
    make_sdk(fingerprint_key=key, fingerprint_key_id=key_id)  # non-strict: falls back safely
    tracer = aobs.get_tracer()
    assert tracer is not None and tracer.fingerprinter is not None
    assert tracer.fingerprinter.key_id.startswith("p")  # random per-process key
    assert aobs.diagnostics()["config_warnings"] >= 1


def test_fingerprinting_can_be_disabled(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(fingerprint_inputs=False)

    @aobs.tool
    def lookup(order_id: str) -> None:
        pass

    lookup("1")
    flush()
    assert "input_fingerprint" not in end_by_name(sdk, "lookup")


def test_key_material_is_not_reused_from_os_environ_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed env key must not be echoed in warnings.
    monkeypatch.setenv("AOBS_FINGERPRINT_KEY", "zz-not-hex-" + os.urandom(4).hex())
    from aobs._diagnostics import Diagnostics
    from aobs.config import build_config

    diag = Diagnostics()
    config = build_config(diag)
    assert "zz-not-hex" not in repr(config)
    assert diag.snapshot()["config_warnings"] == 1
