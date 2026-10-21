# Action safety (`aobs.safety`)

Status: **first version**, part of the Python SDK (standard library only). Not imported by
`import aobs`: use `from aobs import safety`.

An agent that can refund, pay, email or delete must not do it because a model said so, must not
do it twice because a call timed out, and must not retry blindly when nobody knows whether it
happened. `aobs.safety` puts a guard in front of such a tool:

1. **Decide first.** A policy checks the request against trusted evidence (e.g. the order from
   your order system, never the model's arguments): required fields, matching ids, amount limits,
   approval above a threshold, one action per order.
2. **Record before acting.** The action is reserved, then marked `EXECUTING` in a store, before
   the tool is called.
3. **Never retry the unknown.** A timeout or crash leaves the outcome `UNKNOWN`. The guard never
   retries it; one read-only status lookup or a person settles it.
4. **Same key every time.** Every attempt at one logical action sends the provider the same
   idempotency key, so a provider that honours it turns repeats into no-ops.

Decisions and outcomes are recorded as `guardrail` spans in the trace; enforcement does not depend
on telemetry.

## Quick start

`safety_policy.toml`:

```toml
version = 1

[actions.issue_refund]
required_evidence = ["order_id", "amount", "status"]  # must be present in the trusted evidence
must_match = ["order_id"]                             # the request must be for that order
amount = "amount"
amount_at_most_evidence = true                        # never more than the order's amount
max_amount = "200.00"                                 # denied above this
approval_above = "100.00"                             # a person approves above this
once_per = ["order_id"]                               # one refund per order
```

```python
import secrets

from aobs import safety

# In production, load two stable random secrets (32+ bytes each) from your secret manager, the
# same in every process and after every restart. Generated here so the example runs.
STORE_SECRET = secrets.token_bytes(32)
IDEMPOTENCY_SECRET = secrets.token_bytes(32)

guard = safety.ActionGuard(  # one per process
    safety.load_policy("safety_policy.toml"),
    store=safety.SqliteActionStore("actions.db", secret=STORE_SECRET),
    idempotency=safety.IdempotencyKeys(namespace="support-agent/dev", secret=IDEMPOTENCY_SECRET),
)

ORDERS = {"1042": {"order_id": "1042", "amount": 59.90, "status": "delivered"}}  # your order system
LEDGER: dict[str, dict] = {}  # stands in for your payments provider


def send_refund(order_id: str, amount: float, idempotency_key: str) -> dict:
    LEDGER.setdefault(idempotency_key, {"order_id": order_id, "amount": amount})  # one refund per key
    return {"refunded": amount}


refund = guard.protect(
    send_refund,
    action="issue_refund",
    evidence=lambda: ORDERS["1042"],  # trusted: the ticket's order, not the model's arguments
    action_key="refund:ticket-7",  # the identity of this one logical refund
    idempotency_param="idempotency_key",  # the guard fills this in; the model cannot
)

print(refund(order_id="1042", amount=59.90))  # {'refunded': 59.9}

for args in ({"order_id": "1042", "amount": 59.90}, {"order_id": "2077", "amount": 59.90}):
    try:
        refund(**args)
    except safety.ActionBlocked as blocked:
        print(blocked.result.decision.reason_codes, blocked.result.previous_outcome)
# ('duplicate_action',) SUCCEEDED      <- asked again: already done, not repeated
# ('evidence_mismatch',) None          <- the model named another order: denied
```

The wrapper has the tool's own parameters (minus `idempotency_param`) and returns its own result
when the action succeeds. Anything else raises `ActionBlocked`, whose `result` says why; tell the
model the truth from it, for example "already refunded" for `previous_outcome == "SUCCEEDED"`,
never "not allowed". `guard.run(action, request, evidence, execute, ...)` does the same without a
wrapper.

Runnable examples with an agent: `examples/support_agent/safety_demo.py` (twelve scenarios:
denials, approval, rejections, timeouts, duplicates, a restart) and
`examples/support_agent/protect_example.py` (wrapping an existing tool in a few lines).

## Policy

TOML, strictly validated (unknown keys, bad types and contradictions raise `PolicyError`). An
action that is not listed is denied.

| Key | Meaning |
|---|---|
| `required_evidence` | Fields that must be present and non-empty in the evidence. |
| `must_match` | Request fields that must equal the evidence (compared as trimmed text). Must also be in `required_evidence`. |
| `amount` | The amount field, in the request and the evidence. Needed by the three rules below. |
| `amount_at_most_evidence` | The request amount may not exceed the evidence amount. |
| `max_amount` | Denied above this (a decimal written as a string, e.g. `"200.00"`). |
| `approval_above` | `approver` must return exactly `True` above this; otherwise not executed. |
| `once_per` | One action per value of these fields (fields must be in `must_match`, so only verified values count). |

## Results

`ActionResult` fields, and what to do with them:

| Field | Meaning |
|---|---|
| `decision.verdict` | `ALLOW`, `DENY` or `REQUIRE_APPROVAL`, with `reason_codes` and the `rule` that decided. |
| `outcome` | What this attempt did: `NOT_EXECUTED`, `SUCCEEDED`, `FAILED` (the adapter raised `ActionNotPerformed`: it definitely did not happen) or `UNKNOWN` (anything else, timeouts included). |
| `final_outcome` | The outcome to act on: after a read-only `verify`, and the stored, authoritative one if the record changed while this attempt ran (`superseded`). |
| `previous_outcome` | For a duplicate: the earlier attempt's stored state (`SUCCEEDED` means it was already done). |
| `recovery` | `none`, `retry_allowed` (after `FAILED`), `verified_succeeded`, `verified_failed` or `escalate`. The guard itself never retries. |

Reason codes: `all_checks_passed`, `approval_required`, `action_not_permitted`, `invalid_input`,
`evidence_missing`, `request_missing`, `evidence_mismatch`, `amount_invalid`, `evidence_invalid`,
`amount_exceeds_evidence`, `amount_exceeds_limit`, `duplicate_action`, `action_key_conflict` (one
action key, a different request), `action_key_required`, `action_key_invalid`,
`idempotency_unconfigured`, `idempotency_config_changed`, `request_changed`, `store_unavailable`,
`policy_mismatch`.

Write adapters so that they **return only on success**, raise `ActionNotPerformed` only when the
provider definitely did not act (e.g. it refused the request), and let everything else (timeouts,
connection errors) propagate: that becomes `UNKNOWN`.

## Identity and provider idempotency

- `action_key` (from your system, e.g. the ticket id; never from the model) names one logical
  action. The same key always means the same request. A legitimate second refund for the same
  order is a new logical action: a new key, and a policy whose `once_per` includes a trusted
  per-case field.
- `IdempotencyKeys(namespace=..., secret=...)` derives `ctx.idempotency_key` with an HMAC of the
  namespace, action and action key. It is required whenever an `action_key` is used, the same in
  every worker and after restarts, and separate from the store's secret. Use a namespace per
  service and environment (e.g. `"billing/production"`), so deployments sharing a provider account
  never collide.
- Pass the key to the provider (`idempotency_param`, or `ctx.idempotency_key` in `execute` and
  `verify`). **This is what prevents most duplicates**: the store only decides which attempt may
  start.
- Rotation: start new actions with the new configuration and keep the old one until every action
  made under it is settled. The guard refuses to continue an action under another configuration
  (`idempotency_config_changed`), because a new key would let the provider repeat it.
- Some providers remember a *failed* response for a key and return it again: reusing the key after
  `FAILED` may not let the same operation succeed later. Check your provider's rules (and how long
  it keeps keys).

## Stores

| Store | Scope |
|---|---|
| `MemoryActionStore()` (the default) | One process; lost on restart. For development and tests. |
| `SqliteActionStore(path, secret=..., key_id="default")` | Durable on one host; coordinates every thread and process that opens the file. Requires an `action_key` on every action. |

`SqliteActionStore` needs:

- a **random secret** of at least 32 bytes (e.g. `secrets.token_bytes(32)`, stored in a secret
  manager), the same for every process that shares the file. It keys every stored identifier, so
  low-entropy values such as order ids cannot be recovered from the file by guessing. Opening the
  file with a missing or different secret fails before anything runs. Obviously non-random values
  are refused; no check can prove a value is random. There is no in-place rotation: settle the open
  actions and start a new file;
- a **local file system** with working locks and shared memory (not NFS or SMB) and storage that
  honours fsync. Opening fails if WAL mode cannot be enabled;
- a **real path**: symbolic links are refused. New files are created with mode 0600; an existing
  file that other users can read or write is reported (`UnsafePermissionsWarning`,
  `store.private is False`), not changed.

What the file still shows to someone who can read it, even without the secret: action names,
states, timestamps, counts, and which records share a digest. Someone who can write it can change
or delete records. Protect it with file permissions.

For several hosts, implement the `ActionStore` protocol on a shared database (the reservation rule
is `safety.reservation_conflict`); none is included yet.

## Recovery

- `guard.inspect(action, action_key)`: the stored state.
- `guard.reconcile(action, action_key, verify=lookup)`: one read-only lookup for an `EXECUTING`
  (e.g. its process crashed) or `UNKNOWN` action. A confirmed `SUCCEEDED` is recorded at any time.
  While the execution is younger than `stale_after_s` (default 900 s, set it longer than your
  slowest action) nothing else changes it: "not found" may only mean "not sent yet". After that,
  `FAILED` from the lookup is recorded; anything else marks the action `UNKNOWN`, resolution
  `stale`.
- `guard.resolve(action, action_key, "SUCCEEDED" | "FAILED")`: a person's decision about an
  `UNKNOWN` action. For a stale one, `FAILED` (which lets a new attempt run) also requires
  `original_stopped=True`: your statement that the original process has stopped, or that the
  provider honours the idempotency key. It is recorded (`manual_stopped`) but cannot be verified.

`verify` must only read the provider's state. Return `"SUCCEEDED"` only when the action is
confirmed, and `"FAILED"` only when the provider guarantees the request can no longer complete;
anything else leaves it unknown.

## What is and is not guaranteed

- `execute` is called at most once per `run`, only after the decision allows it, with exactly the
  checked arguments. Denied, unapproved and duplicate actions never run, also when telemetry is off
  or failing.
- With a shared durable store, a new attempt for an action key starts only while no earlier one is
  running, succeeded or unknown, i.e. after `FAILED` is recorded (by the adapter, a lookup or a
  person).
- **Not exactly-once.** An earlier attempt that was only paused can still reach the provider after
  another attempt took over. Whether that becomes a second side effect depends on the provider
  honouring the idempotency key. Without provider idempotency or a reliable status lookup, unknown
  outcomes stay unknown until a person resolves them.
- The memory store gives none of this across processes or restarts; SQLite gives it on one host.
- Calls that bypass the guard (calling the original function directly) are not prevented.
- Synchronous tools only: async and generator functions are refused before anything runs.
- Evidence values, action keys, idempotency keys and secrets are never written to traces.
