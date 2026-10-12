"""Usage and cost stay unknown unless actually reported; nothing is zero-filled or invented."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import aobs
from aobs import _sanitize as S
from aobs.export import InMemoryExporter
from sdk_support import end_by_name, flush, only_trace


def test_no_usage_reported_means_no_usage_field(sdk: InMemoryExporter) -> None:
    with aobs.llm_span(provider="acme", request_model="m"):
        pass
    flush()
    event = end_by_name(sdk, "chat")
    assert "usage" not in event and "cost" not in event
    total = only_trace(sdk).tokens["input_tokens"]
    assert total.value is None and total.expected_spans == 1 and not total.complete


def test_partial_usage_keeps_missing_counts_unknown(sdk: InMemoryExporter) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_usage(input_tokens=100)  # provider gave no output count
    flush()
    assert end_by_name(sdk, "chat")["usage"] == {"input_tokens": 100, "source": "provider_reported"}
    tokens = only_trace(sdk).tokens
    assert tokens["input_tokens"].value == 100 and tokens["input_tokens"].complete
    assert tokens["output_tokens"].value is None


@pytest.mark.parametrize("bad", [-1, 1.5, True, "12", 2**60, None])
def test_invalid_counts_are_dropped_not_coerced(sdk: InMemoryExporter, bad: Any) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_usage(input_tokens=bad, output_tokens=5)
    flush()
    assert end_by_name(sdk, "chat")["usage"] == {"output_tokens": 5, "source": "provider_reported"}
    if bad is not None:
        assert aobs.diagnostics()["invalid_usage_values"] == 1


def test_all_counts_invalid_means_no_usage(sdk: InMemoryExporter) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_usage(input_tokens=-5)
    flush()
    assert "usage" not in end_by_name(sdk, "chat")


def test_estimated_usage_is_labelled(sdk: InMemoryExporter) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_usage(input_tokens=40, source="sdk_estimated")
    flush()
    assert end_by_name(sdk, "chat")["usage"]["source"] == "sdk_estimated"
    assert only_trace(sdk).tokens["input_tokens"].estimated


def test_unknown_usage_source_is_rejected(sdk: InMemoryExporter) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_usage(input_tokens=40, source="guess")
    flush()
    assert "usage" not in end_by_name(sdk, "chat")


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0.000731", "0.000731"),
        (Decimal("1.50"), "1.5"),
        (2, "2"),
        ("1e-3", "0.001"),
        (0.1, "0.1"),
        ("0.0000000000004", "0"),
        ("12.3456789012345", "12.345678901234"),
    ],
)
def test_provider_reported_cost_is_normalized(sdk: InMemoryExporter, amount: Any, expected: str) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_cost(amount, "usd")
    flush()
    assert end_by_name(sdk, "chat")["cost"] == {"amount": expected, "currency": "USD", "source": "provider_reported"}


@pytest.mark.parametrize("amount", [-1, "abc", float("nan"), float("inf"), True, "1" * 20])
def test_invalid_cost_is_dropped(sdk: InMemoryExporter, amount: Any) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_cost(amount)
    flush()
    assert "cost" not in end_by_name(sdk, "chat")
    assert aobs.diagnostics()["invalid_cost_values"] == 1


def test_invalid_currency_is_dropped(sdk: InMemoryExporter) -> None:
    with aobs.llm_span(provider="acme") as sp:
        sp.record_cost("1.0", "dollars")
    flush()
    assert "cost" not in end_by_name(sdk, "chat")


def test_money_helper_never_uses_exponent() -> None:
    assert S.money(Decimal("1E+3")) == "1000"
    assert S.money("100") == "100"
