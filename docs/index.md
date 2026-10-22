# aobs — observability for AI agents

aobs records what an AI agent did — model calls, tool calls, sub-agents, errors, latency, tokens
and cost — as nested traces, whatever framework or model provider the agent uses. It then explains
the runs, checks them against requirements you write down, and guards the tools that have real
side effects.

> **Status:** pre-release (`0.1.0.dev0`), not yet published to PyPI: install from a clone of the
> repository. Not production-hardened: the collector has no rate limiting, retention job or TLS
> of its own yet. See the [roadmap](roadmap.md) for what is done and what is planned.

## What it does

**Tracing.** The Python SDK (`aobs`, standard library only) turns a run of your agent into one
trace: `aobs.run(...)` for the run, `@aobs.tool` for tools, and a wrapper for OpenAI-compatible
Chat Completions clients (sync, async and streaming). Traces go to a local JSONL file or to the
collector (`aobs-server serve`), which authenticates with API keys, applies the project's privacy
policy before storage and stores runs in PostgreSQL.
[Python SDK](sdk.md) · [Ingestion API](ingestion.md) · [Event model](event-model.md)

**Diagnosis.** `aobs-server analyze` explains a run with deterministic rules (errors, repeated
failures, wasted calls, bottlenecks), each with evidence and a suggestion; `compare` and
`compare-groups` find regressions between runs, with exact statistical tests for repeated runs.
They read trace files or the collector's stored traces, and a read-only [dashboard](dashboard.md)
shows the same results in a browser.
[Analyzing stored traces](stored-analysis.md)

**Reliability gate in CI.** `aobs-server gate` checks recorded runs against a TOML policy
(outcome, duration, calls, retries, tokens, cost, required and forbidden tools, pass rate,
baseline limits) and answers PASS or FAIL with the spans that show why. It exits with code 3 on
FAIL and writes JUnit XML and Markdown reports for CI systems.
[Reliability gate](reliability-gate.md)

**Action safety.** `aobs.safety` puts a guard in front of tools that refund, pay, email or
delete: a policy checks the request against trusted evidence before anything happens, approval
above a threshold, duplicate prevention, a durable SQLite record before the call, no blind retries
when the outcome is unknown, and the same provider idempotency key on every attempt. It is not
exactly-once; its guarantees and limits are stated in the guide.
[Action safety](action-safety.md)

## Where to start

- [Quick start](getting-started.md): instrument your own agent, then analyze and gate its runs
  (no server or database needed).
- [End-to-end workflow](agent-workflow.md): run the example agent with a scripted model (no API
  key), send its runs to the collector, then list, analyze, compare, gate and view them.
- [Architecture](architecture.md) and [decisions](decisions.md): how the pieces fit and why.

The source code and issue tracker are on
[GitHub](https://github.com/AshmitJain932/ai-agent-observability). Licensed under Apache 2.0.
