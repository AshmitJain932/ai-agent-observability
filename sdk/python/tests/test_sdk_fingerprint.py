"""Keyed input fingerprints: correct format, stable within a key, and the key never leaves."""

from __future__ import annotations

import base64
import os
import pickle
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

import aobs
from aobs.config import ConfigError
from aobs.export import InMemoryExporter
from aobs.fingerprint import Fingerprinter, Unfingerprintable
from sdk_support import check_bodies, end_by_name, ends, flush

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


# --------------------------------------------------------------------------- stable canonical form

GOLDEN_KEY, GOLDEN_KEY_ID = b"k" * 32, "golden"


class _Dumps:
    def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict[str, object]:
        return {"b": 2, "a": [1, "x"]}


class _StableRepr:
    def __repr__(self) -> str:
        return "StableRepr(id=7)"


# Digests recorded before fingerprints got their own canonical form: values that already had a
# stable representation must keep exactly the same fingerprint (comparable under a configured key).
GOLDEN = {
    "dict": (
        {"order_id": "1042", "qty": 2, "ok": True, "price": 9.5, "none": None},
        "05922c33b8215da627f44c0b7a282d02c98f9b33a15dec885a73b3f45435d3a1",
    ),
    "list": ([1, "two", 3.0, None, False], "118405a3bf86ebcd8ce9d497492995ba45648c260b50651a145d44d6743435c1"),
    "tuple_as_list": ((1, "two"), "cf037466deb2b654251cd83ea611b100749617d5417067de3dd89c908b7cc210"),
    "nested": (
        {"a": {"b": [1, {"c": "d"}]}, "z": []},
        "00ceaf6de127bb5b26cb58da9d913331b051c43b4f670f347d1294d5a2a1bcbf",
    ),
    "unicode": ("Grüße, 世界", "d5c1f438af6ccdb349784f81b8f5c8db1d8aa1648dc79a66408a63550d98390a"),
    "non_finite": (float("inf"), "9c4bfab0c426b698b1ca4aeb5eed3fe08240db7ac90a0b10e647c350d35e14b6"),
    "int_keys": ({1: "a", 2: "b"}, "5849625b5d51528750030c151e1425cb65c6c64fa472a37e3fd9eb51c2ce0113"),
    "decimal": (Decimal("1.50"), "6097a510392ad2849d20ee287a4e6944109bf832321b6a638e2f8156e565c72b"),
    "date": (date(2026, 10, 14), "831ecbd8aad21cd2a34715dbceb7f6cb8b161c6ba2e25410e521784aaf32bc3a"),
    "model_dump": (_Dumps(), "3f8e163fa9e8b057031e05ed01d87b3f495f99d39151d24bf827ffa0b90844e1"),
    "stable_repr": (_StableRepr(), "f280c62a1a4134b917f4a8851e66659df6631c3168adfef61871be9ca1993ee2"),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_fingerprints_of_stable_values_are_unchanged(name: str) -> None:
    value, digest = GOLDEN[name]
    assert Fingerprinter(GOLDEN_KEY, GOLDEN_KEY_ID).fingerprint(value) == f"hmac-sha256:{GOLDEN_KEY_ID}:{digest}"


class Order:
    """No __repr__: its default text form is its memory address."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id


def test_objects_without_a_stable_form_get_no_fingerprint_instead_of_a_wrong_one(sdk: InMemoryExporter) -> None:
    @aobs.tool
    def refund(order: Order) -> None:
        return None

    with aobs.run("r"):
        for order_id in ["1042", "9999", "5555", "1042"]:  # a fresh object per call, as in real code
            refund(Order(order_id))
    flush()
    calls = [e for e in ends(sdk) if e["name"] == "refund"]
    assert len(calls) == 4
    assert all("input_fingerprint" not in e for e in calls)  # unknown, never falsely equal or unequal
    assert aobs.diagnostics()["fingerprints_unavailable"] == 4
    check_bodies(sdk.bodies)


@dataclass
class OrderRecord:
    order_id: str


def test_values_with_a_stable_repr_still_get_comparable_fingerprints(sdk: InMemoryExporter) -> None:
    @aobs.tool
    def refund(order: OrderRecord) -> None:
        return None

    with aobs.run("r"):
        for order_id in ["1042", "9999", "5555", "1042"]:
            refund(OrderRecord(order_id))
    flush()
    fps = [e["input_fingerprint"] for e in ends(sdk) if e["name"] == "refund"]
    assert fps[0] == fps[3]  # the same order twice
    assert len({fps[0], fps[1], fps[2]}) == 3  # different orders
    assert aobs.diagnostics()["fingerprints_unavailable"] == 0


@pytest.mark.parametrize(
    "value",
    [
        object(),
        Order("1"),
        Order("1").__init__,  # a bound method shows its object's address (a builtin like print does not)
        lambda: None,
        threading.Lock(),
        {"client": Order("1")},
        [1, Order("1")],
        {Order("1"): "as a key"},
        {"tags": frozenset({Order("1")})},
    ],
)
def test_values_without_a_stable_form_are_refused(value: object) -> None:
    with pytest.raises(Unfingerprintable):
        Fingerprinter(GOLDEN_KEY, GOLDEN_KEY_ID).fingerprint(value)


def test_nesting_beyond_the_limit_is_refused_instead_of_colliding() -> None:
    deep: object = "leaf"
    for _ in range(40):
        deep = [deep]
    with pytest.raises(Unfingerprintable):
        Fingerprinter(GOLDEN_KEY, GOLDEN_KEY_ID).fingerprint(deep)


def test_strings_that_look_like_addresses_are_data() -> None:
    fp = Fingerprinter(GOLDEN_KEY, GOLDEN_KEY_ID)
    assert PATTERN.match(fp.fingerprint({"note": "<object at 0x7f00>"}))  # strings are inputs, not reprs


def test_set_element_order_does_not_matter() -> None:
    fp = Fingerprinter(GOLDEN_KEY, GOLDEN_KEY_ID)
    a = fp.fingerprint({"tags": {"alpha", "beta", "gamma", "delta"}, "ids": {3, 1, 2}})
    b = fp.fingerprint({"tags": frozenset(["delta", "gamma", "beta", "alpha"]), "ids": frozenset([2, 3, 1])})
    assert a == b
    assert a != fp.fingerprint({"tags": {"alpha", "beta", "gamma"}, "ids": {3, 1, 2}})


SEED_PROBE = """
import json
from aobs.fingerprint import Fingerprinter
value = {"tags": {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}, "mixed": {"x", 1, ("t", 2), 2.5}}
print(Fingerprinter(b"k" * 32, "seed").fingerprint(value))
"""


def test_set_fingerprints_do_not_depend_on_the_hash_seed() -> None:
    results = set()
    for seed in ("0", "1", "2", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", SEED_PROBE], env=env, capture_output=True, text=True, check=True)
        results.add(out.stdout.strip())
    assert len(results) == 1 and PATTERN.match(results.pop())
