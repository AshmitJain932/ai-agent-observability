"""A simulated payments API for the action-safety demo and tests. Nothing here leaves the process:
refunds are entries in an in-memory ledger.

Modes reproduce what a real payments API can do to a caller:

* ``ok`` — the refund is made and confirmed.
* ``rejected`` — the API refuses the request before doing anything (e.g. a declined card). The
  adapter reports this as :class:`aobs.safety.ActionNotPerformed`.
* ``timeout_before_commit`` — the call times out and the provider aborts the request: the refund
  was **not** made, and the status lookup says so authoritatively (``FAILED``).
* ``timeout_after_commit`` — the refund **was** made, then the response was lost (timeout).
* ``timeout_pending`` — the call times out while the provider is still processing the request:
  the status lookup says ``PENDING``, and :meth:`MockPayments.complete_pending` makes the refund
  later. A "not found yet" is not a failure.

In every timeout mode the caller cannot tell which case it is in. ``status()`` is a read-only
lookup by idempotency key (``lookup_available=False`` simulates an API without one): ``SUCCEEDED``,
``FAILED`` (aborted: it can no longer happen), ``PENDING`` or ``NOT_FOUND``. Refunds are keyed by
idempotency key, so the same key never refunds twice; different keys for the same order do, which
is exactly the duplicate the safety layer has to prevent.

A real provider's idempotency is what makes a repeated request harmless; the safety layer's store
only decides which attempt may start. Check the provider's rules: some keep a key only for a limited
time, and some remember a *failed* response for a key and return it again, so reusing that key may
not let the same refund succeed later (this mock forgets aborted keys, so a retry can succeed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Mode = Literal["ok", "rejected", "timeout_before_commit", "timeout_after_commit", "timeout_pending"]


class PaymentsTimeout(TimeoutError):
    """The payments API did not answer in time; the refund may or may not have been made."""


class PaymentsRejected(Exception):
    """The payments API refused the request; nothing was done."""


@dataclass
class MockPayments:
    mode: Mode = "ok"
    lookup_available: bool = True
    ledger: dict[str, dict[str, Any]] = field(default_factory=dict)  # idempotency key -> refund
    aborted: set[str] = field(default_factory=set)  # idempotency keys the provider gave up on
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)  # accepted, not yet processed
    refund_calls: int = 0
    status_calls: int = 0

    def refund(self, order_id: str, amount: float, idempotency_key: str) -> dict[str, Any]:
        self.refund_calls += 1
        if self.mode == "rejected":
            raise PaymentsRejected("refund declined by the card issuer")
        refund = {"order_id": order_id, "amount": amount, "refund_id": f"rf_{order_id}"}
        if self.mode == "timeout_before_commit":
            self.aborted.add(idempotency_key)
            raise PaymentsTimeout("payments API did not respond within 2s")
        if self.mode == "timeout_pending" and idempotency_key not in self.ledger:
            self.pending.setdefault(idempotency_key, refund)
            raise PaymentsTimeout("payments API did not respond within 2s")
        self.aborted.discard(idempotency_key)
        if idempotency_key not in self.ledger:
            self.ledger[idempotency_key] = self.pending.pop(idempotency_key, refund)
        if self.mode == "timeout_after_commit":
            raise PaymentsTimeout("payments API did not respond within 2s")
        return {"order_id": order_id, "refunded": amount, "reference": self.ledger[idempotency_key]["refund_id"]}

    def status(self, idempotency_key: str) -> str:
        """Read-only, never refunds: SUCCEEDED, FAILED (aborted; it cannot happen any more),
        PENDING (still processing) or NOT_FOUND (nothing known, which proves nothing)."""
        self.status_calls += 1
        if not self.lookup_available:
            raise ConnectionError("this payments API has no status lookup")
        if idempotency_key in self.ledger:
            return "SUCCEEDED"
        if idempotency_key in self.aborted:
            return "FAILED"
        return "PENDING" if idempotency_key in self.pending else "NOT_FOUND"

    def complete_pending(self) -> None:
        """The provider finishes the requests it accepted before timing out."""
        for key in list(self.pending):
            self.ledger[key] = self.pending.pop(key)

    def refunds_for(self, order_id: str) -> int:
        return sum(1 for refund in self.ledger.values() if refund["order_id"] == order_id)
