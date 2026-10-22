# Action-safety recipes

Small, self-contained examples of [`aobs.safety`](../../docs/action-safety.md) guarding the risky
tools agents are most often given. Each one is a single file with its own policy, runs offline in
under a second, and prints what was allowed or blocked and why (the guard's reason codes).

```bash
uv sync
uv run python examples/safety_recipes/send_email.py
uv run python examples/safety_recipes/delete_records.py
uv run python examples/safety_recipes/deploy.py
```

Everything is simulated: the support system, CRM, CI and mail and deploy APIs are fakes in memory,
and each recipe's action store is a SQLite file in a temporary directory. No email is sent, no data
is deleted and nothing is deployed. The model's tool calls are scripted (each step is one call), so
the recipes show the guard, not a model.

| Recipe | Policy | What it shows |
|---|---|---|
| [`send_email.py`](send_email.py) | [`send_email_policy.toml`](send_email_policy.toml) | One email per (ticket, template), even when the agent loops, retries after a timeout or is restarted; the recipient must be the ticket's customer on record, not an address the model was given (`evidence_mismatch`); an unknown template is denied (`evidence_missing`); a timeout is settled by one read-only status lookup by idempotency key, never by sending again. |
| [`delete_records.py`](delete_records.py) | [`delete_records_policy.toml`](delete_records_policy.toml) | Deleting a customer's data only if every record exists (`amount_exceeds_evidence`) and belongs to the signed-in account (`evidence_mismatch`); more than 25 records need the privacy officer's approval (`approval_required`, declined then granted); more than 100 are refused (`amount_exceeds_limit`). |
| [`deploy.py`](deploy.py) | [`deploy_policy.toml`](deploy_policy.toml) | Deploying only the commit CI marked green (`evidence_mismatch` for the newest, unverified one; `evidence_missing` when no build is green); each commit at most once per (service, environment), also for a second release request and after a restart (`duplicate_action` on `once_per`); production needs the on-call engineer's approval. |

There is no payments recipe: [`examples/support_agent/safety_demo.py`](../support_agent/safety_demo.py)
already covers money (amount limits against the order, approval above a threshold, rejections,
timeouts with and without a status lookup, duplicates and a restart) with a simulated payments API.

## The pattern

Every recipe follows the same steps; the policy file says what "allowed" means.

1. **Evidence from a system of record**, never from the model's arguments: the ticket's customer,
   the record owners in the CRM, the commit CI marked green. The model's request must match it
   (`must_match`). Where the model names *what* to act on (the records to delete), the facts about
   those things still come from the system of record, and the identity they must match (the
   signed-in account) comes from the session.
2. **An action key from your system** (the ticket, deletion request or release request id) names
   each logical action, so a repeated call is a duplicate instead of a second side effect. The
   policy's `once_per` adds a duplicate check on verified values (one email per ticket and
   template; one deploy per service, environment and commit, whatever the release request).
3. **A durable store** (`SqliteActionStore`, here in a temporary directory) remembers what ran
   across restarts. In production it needs a stable random secret from your secret manager, the
   same in every process.
4. **The same idempotency key on every attempt** (`ctx.idempotency_key`, from `IdempotencyKeys`)
   goes to the provider, and `verify` looks the action up by it: the fakes here honour it, as your
   provider must for a repeated request to be a no-op.
5. **Thresholds and a person**: `approval_above` asks the `approver` callback, which must return
   exactly `True`; `max_amount` refuses outright. The threshold is numeric, so the deploy recipe
   gives each environment a numeric approval level in its deploy-target registry (staging 1,
   production 2).
6. **Tell the model the truth** from the result: "already sent" for a duplicate whose earlier
   attempt succeeded, "not approved", "not allowed" with the reason codes, or "outcome unknown, do
   not retry".

What this does not give you is the same as for the guard itself
([docs/action-safety.md](../../docs/action-safety.md#what-is-and-is-not-guaranteed)): it is not
exactly-once execution, `SqliteActionStore` coordinates processes on one host only, and calling a
tool function directly, around the guard, is not prevented.

Tested by `sdk/python/tests/test_sdk_safety_recipes.py`, which runs each recipe and checks every
step's decision and outcome and what the fake backends did.
