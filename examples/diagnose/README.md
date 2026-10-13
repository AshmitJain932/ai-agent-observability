# Demo: diagnose an agent run from its trace

`aobs-server analyze` reads saved traces and prints a report: the execution tree, the tool calls,
timings, and findings. Each finding comes with the evidence that supports it (span ids and
recorded facts) and a debugging suggestion.

The rules are deterministic. The command needs no database, no collector server and no LLM or
API key. It runs entirely on the files the SDK writes.

## Run it

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11, from the repository root:

```bash
uv sync

# 1. Diagnose the sample traces that ship with the repository
uv run aobs-server analyze examples/diagnose/traces/refund_outage.jsonl
uv run aobs-server analyze examples/diagnose/traces/retry_storm.jsonl
uv run aobs-server analyze examples/diagnose/traces/slow_and_repeated.jsonl
uv run aobs-server analyze examples/diagnose/traces/crashed_run.jsonl
uv run aobs-server analyze examples/diagnose/traces/healthy_run.jsonl

# 2. Run the instrumented example agent yourself, then diagnose what it recorded
uv run python examples/support_agent/run.py --scenario refund-outage --out my_run.jsonl --quiet
uv run aobs-server analyze my_run.jsonl

# 3. Re-record all samples with the real SDK (written to examples/diagnose/traces/ by default)
uv run python examples/diagnose/make_samples.py --out my_samples
uv run aobs-server analyze my_samples/*.jsonl
```

Useful options: `--json` for machine-readable output, `--trace <id prefix>` to pick one trace,
`--no-error-messages` to omit error text, and `--fail-on error|warning|info`, which exits with
code 3 when a finding is that severe (useful in CI). `--slow-share` and `--slow-min-ms` tune the
bottleneck rule. Exit codes: 0 report printed, 1 no traces or unreadable input, 2 invalid options,
3 `--fail-on` reached.

## The samples

| File | What happened | Findings |
|---|---|---|
| `refund_outage.jsonl` | The Phase 2 support agent; the payments API times out and the timeout fails the run | `error.root_cause` |
| `retry_storm.jsonl` | An inventory agent retries a refused database connection 3 times, then gives up | `error.root_cause`, `error.repeated`, `data.missing_usage` |
| `slow_and_repeated.jsonl` | A research agent fetches the same profile 3 times and waits 0.4 s on a search | `waste.repeated_call`, `latency.bottleneck` |
| `crashed_run.jsonl` | The process dies during a payment call: the run's start was recorded, its end never was | `trace.unfinished` |
| `healthy_run.jsonl` | The support agent answers normally | none |

## Rules

| Rule | Severity | Fires when |
|---|---|---|
| `error.root_cause` | error | A span failed and none of its children did. Identical failures are grouped, and the report says whether the error reached the run |
| `error.repeated` | error | The same operation failed with the same error type at least twice in one run. Retry attempts and identical inputs are shown |
| `waste.repeated_call` | warning | At least two *successful* calls had identical input (same keyed input fingerprint) |
| `latency.bottleneck` | warning | A span took ≥ 50 % of the run and ≥ 100 ms. The deepest such span is reported |
| `trace.unfinished` | warning | A span, possibly the run itself, has a start but no end |
| `trace.missing_parent`, `trace.parent_cycle` | warning | Parent links point to a span that never arrived, or form a loop |
| `data.missing_usage` | info | Model calls reported no token usage, so token totals are a lower bound |
| `data.clock_skew` | info | A child lies outside its parent's time range (clocks on different hosts disagree) |

## Privacy

Every file goes through the collector's own steps before anything is shown:

- strict parsing and schema validation;
- the ingest privacy step with a `metadata_only` ceiling, which drops prompts, responses, tool
  arguments and results, and scrubs secrets from error messages and attributes.

`retry_storm` and `slow_and_repeated` were recorded with `capture="full"`, so their files contain
prompts and arguments. The report shows none of them; the tests check this. Error *types* are
always shown. Error *messages* are shown after scrubbing, or not at all with
`--no-error-messages`.

## What this does and does not show

It shows a complete, offline path: SDK instrumentation, the JSONL file, validation (the Phase 1
model), privacy enforcement (M4), trace assembly, diagnosis, and the report.

It does **not** use the collector's HTTP ingestion or PostgreSQL storage. Those are not wired up
yet (Phase 3, M5/M6). The rules work on an assembled trace, so they will run on stored traces
unchanged once that exists.
