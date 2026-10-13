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

---

# Demo: compare runs and find regressions

`aobs-server compare BASELINE CURRENT [CURRENT ...]` compares a known-good run with later runs of
the same task. It reports what changed:

- the run now fails (or is fixed);
- new errors and more failures;
- slower runs and slower operations;
- tools added, removed or called more often;
- more retries;
- a different model;
- more tokens;
- higher provider-reported cost.

It also says which differences it could *not* measure reliably. Like `analyze`, it needs no
database, server or LLM, and it never shows content.

## Run it

```bash
# A release that made the inventory agent slower, costlier and flaky
uv run aobs-server compare examples/diagnose/compare/inventory_v1.jsonl examples/diagnose/compare/inventory_v2.jsonl

# A run that used to succeed and now fails
uv run aobs-server compare examples/diagnose/compare/refund_ok.jsonl examples/diagnose/compare/refund_outage.jsonl

# The same healthy task recorded twice: no regressions (timing noise stays below the thresholds)
uv run aobs-server compare examples/diagnose/compare/refund_ok.jsonl examples/diagnose/compare/refund_ok_rerun.jsonl

# One baseline against several later runs, as JSON
uv run aobs-server compare examples/diagnose/compare/refund_ok.jsonl \
    examples/diagnose/compare/refund_ok_rerun.jsonl examples/diagnose/compare/refund_outage.jsonl --json

# Your own runs: record a baseline and a current run with the example agent, then compare them
uv run python examples/support_agent/run.py --scenario refund --out my_baseline.jsonl --quiet
uv run python examples/support_agent/run.py --scenario refund-outage --out my_current.jsonl --quiet
uv run aobs-server compare my_baseline.jsonl my_current.jsonl

# Re-record the sample pairs with the real SDK
uv run python examples/diagnose/make_compare_samples.py --out my_pairs
```

**Options**
- `--baseline-trace ID` and `--current-trace ID` pick a run by trace id prefix when a file holds
  several runs. Without them such a file is rejected, and the runs in it are listed.
- `--json` gives machine-readable output; `--no-error-messages` omits error text.
- `--fail-on error|warning|info` exits with code 3 when a finding is that severe.
- `--min-change-pct` (default 20), `--min-run-ms` (100) and `--min-op-ms` (50) set how large a
  difference must be before it is reported.

**Exit codes:** 0 report printed, 1 unreadable input or no runs in a file, 2 invalid options or
ambiguous run selection, 3 `--fail-on` reached.

## Reading the report

The top table puts the two runs side by side: status, duration, spans, failures, retries, tool
calls, tokens, models and provider-reported cost. Each finding then has:

| Line | Meaning |
|---|---|
| `Measured: baseline X -> current Y [difference]` | Values read from the traces, with units (ms, calls, tokens, currency) |
| `Evidence` | The operations and span ids the finding is based on. Inspect them with `aobs-server analyze <file>` |
| `Why it matters` | The likely impact |
| `Possible explanations (not verified)` | Hypotheses to check, **not** conclusions: a difference between two runs does not show its cause |
| `Next step` | Where to look first |

Example, from `inventory_v1` → `inventory_v2`:

```
[WARNING] the run is slower   (regression.slower_run)
   Measured: baseline 64.2 ms  ->  current 542.8 ms   [+478.6 ms (+745%)]
   Evidence:
     - tool 'check_supplier': total time new, 250.3 ms (+250.3 ms)
     - llm 'summarize': total time 30.6 ms -> 200.2 ms (+169.6 ms)
```

That means: the run took about half a second longer, and most of that time was spent in a new
supplier call and a slower summary step.

| Rule | Severity | Reported when |
|---|---|---|
| `regression.run_failed` / `improvement.run_fixed` | error / info | Final status changed between success (`ok`, `ok_with_errors`) and failure |
| `regression.new_error`, `regression.more_failures` | error | An operation fails with an error type it did not have before, or fails with it more often |
| `regression.slower_run` | warning | Run duration up ≥ 20 % **and** ≥ 100 ms. Evidence shows which operations' total time grew |
| `regression.slower_operation` | warning | An operation's median per-call duration up ≥ 20 % **and** ≥ 50 ms |
| `regression.more_calls`, `regression.more_retries` | warning | More calls to the same operation; more retry attempts (`retry.attempt > 1` or a `retry_of` link) |
| `change.tool_added`, `change.tool_removed`, `change.operation_*` | info | An operation exists in only one of the runs |
| `change.model` | info | The same operation used a different model |
| `regression.more_tokens` | warning | Input or output tokens up ≥ 20 %, only if **every** model call in both runs reported usage |
| `regression.higher_cost` | warning | Provider-reported cost up ≥ 20 %, only if every model call in both runs reports cost in the same currency. Prices are never estimated |
| `compare.different_task` | warning | Run name or agent differs, so the runs may not be comparable |
| `data.unfinished_run`, `data.incomplete_usage` | warning / info | Data that is missing prevents a comparison. That comparison is skipped, not guessed |

## How the comparison stays fair

- **Matching:** operations are matched by span kind and tool or span name. Span ids are random per
  run and are never compared. Several calls with the same name are compared as a group (count,
  failures, median duration), wherever they sit in the tree.
- **Thresholds:** times need both a relative and an absolute threshold, so a 2 ms run getting
  1 ms slower is not a regression. Two separately recorded runs of the same healthy task produce
  no findings (`refund_ok` vs `refund_ok_rerun`), and the tests check this.
- **Failed baselines:** if the baseline failed and the current run succeeds, extra time, calls,
  tokens or cost are expected (the baseline stopped early). They are shown as info only.
- **Unfinished runs:** if either run never finished, duration and outcome are not compared.
- **Usage and cost:** token totals and cost are compared only when complete in both runs.
  Otherwise the report says what was missing.

## Limitations

- **One run per side.** Timings vary between runs, so confirm a slowdown by repeating both runs. There
  are no statistics over repeated runs yet.
- **Matching is by name only.** Two different calls that share a tool name are grouped, and a renamed tool
  appears as one removed and one added.
- **"Different task" is a heuristic.** It only checks the run name and agent. It cannot tell that two
  `answer_ticket` runs answered different questions.
- **Input fingerprints are not compared across runs.** The SDK's default fingerprint key is random per
  process.
