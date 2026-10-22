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

`traces/`, `compare/` and `groups/` are also installed with `aobs-server`, as the dashboard's sample
datasets (`server/src/aobs_server/dashboard/samples/`). After re-recording them here, copy them
there too; `server/tests/test_demo.py` fails while the two differ.

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

It does **not** use the collector's HTTP ingestion or PostgreSQL storage. The collector can now
store runs ([docs/ingestion.md](../../docs/ingestion.md)), but diagnosis does not read them yet. The
rules work on an assembled trace, so they will run on stored traces unchanged once a read path
exists.

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

---

# Demo: repeated runs, with statistics

A single comparison can't tell a real slowdown from an unlucky run.
`aobs-server compare-groups` takes **several recordings of the same task** before and after a
change. It reports, metric by metric, whether the difference is larger than ordinary run-to-run
variation, and whether it is large enough to matter. It needs no database, server or LLM, and it
never shows content.

```
aobs-server compare-groups --baseline FILE [FILE ...] --current FILE [FILE ...] [options]
```

Every run in the `--baseline` files forms one group, and every run in the `--current` files the
other. Files may hold one run or many; the SDK's JSONL exporter appends each run.

## Run it

```bash
# Normal variation: two groups of healthy runs. Expected: no findings
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/healthy_rerun.jsonl

# A genuine slowdown across 10 runs. Expected: LIKELY REGRESSION in run duration and search duration
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/slow.jsonl

# More failures and retries. Expected: LIKELY REGRESSION in failed runs, retries and calls, plus a new error type
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/flaky.jsonl

# One 1.5 s cold start among 10 runs. Expected: no regression claimed; the outlier is listed
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/outlier.jsonl

# Only 3 current runs. Expected: INSUFFICIENT DATA (the slowdown is shown, but no conclusion is drawn)
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/few_runs.jsonl

# 6 of 10 runs lack usage and cost on one model call. Expected: tokens and cost INSUFFICIENT DATA, with exclusions counted
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/partial_usage.jsonl

# Different tasks in the two groups. Expected: comparability warnings
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/compare/refund_ok.jsonl examples/diagnose/compare/refund_ok_rerun.jsonl

# Machine-readable output, and exit code 3 when a likely regression is found (for CI)
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/slow.jsonl --json
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/slow.jsonl --fail-on error

# Re-record all groups with the real SDK (about 10 s)
uv run python examples/diagnose/make_group_samples.py --out my_groups
```

**Options**

| Option | Default | Meaning |
|---|---|---|
| `--task NAME` | — | Keep only runs whose run (root span) name is NAME |
| `--min-runs N` | 5 | Usable runs needed in *each* group before any test is run |
| `--alpha A` | 0.05 | Significance level, applied to the Holm-adjusted p-values |
| `--min-change-pct` | 20 | Practical threshold: relative change |
| `--min-run-ms` | 50 | Practical threshold: absolute change in run duration |
| `--min-op-ms` | 20 | Practical threshold: absolute change in an operation's per-call duration |
| `--json`, `--no-error-messages`, `--fail-on` | — | As for `analyze` and `compare` |

**Exit codes:** 0 report printed, 1 unreadable input or no runs, 2 invalid options, 3 `--fail-on`
reached. With `--fail-on error`, any likely regression triggers it. With `--fail-on warning`, so does
a weak signal in the worse direction, or a comparability warning.

## What is measured

**Per run** (finished runs only):
- total duration;
- whether the run failed;
- retry attempts;
- tool calls;
- whether it repeated an identical call;
- input and output tokens, only if every model call reported them;
- provider-reported cost, only if every model call reported it, in a single currency.

**Per operation:** matched across runs by span kind and tool or span name, never by span id. For
each operation: its median duration per call, calls per run, and whether it failed in the run.

Model changes, added or removed operations, and new error types are listed under *Other changes*.
They are descriptive and not tested.

## Reading the report

The tables show, for every metric:
- the number of usable runs in each group (`n b/c`);
- the baseline and current centre: the median, or the share of runs for proportions;
- the change, absolute and relative;
- the Holm-adjusted p-value;
- a verdict.

Each finding then spells out:

- **baseline / current:** the median with its interquartile range (IQR, the middle 50 % of runs) and
  `n`; for proportions, `k/n` with a 95 % Wilson interval.
- **change:** the change in the median (for counts per run, in the mean), and the Hodges–Lehmann
  *shift estimate*. That is the typical difference between a current run and a baseline run, with
  its 95 % confidence interval. An interval that excludes 0 means the direction is clear.
- **test:** which test was used, the raw p-value, the Holm-adjusted p-value, and how many tests share
  the correction.
- **evidence:** trace ids of the highest current runs and of outliers, plus how many runs were excluded
  and why. Run `aobs-server analyze` on those files to look at individual runs.

| Verdict | Meaning |
|---|---|
| `LIKELY REGRESSION` | Holm-adjusted p < alpha **and** the change exceeds the practical threshold, in the worse direction |
| `POSSIBLE IMPROVEMENT` | The same, in the better direction |
| `WEAK SIGNAL` | The change is large enough to matter, but the evidence is not conclusive; record more runs |
| `MINOR CHANGE` | Statistically detectable, but smaller than the practical threshold |
| `no change` | Neither |
| `INSUFFICIENT DATA` | Fewer than `--min-runs` usable runs in a group: numbers are shown, no test is run, no interval is given |

Statistical evidence and practical impact are separate questions. A `MINOR CHANGE` (say, +3 ms
on 100 ms, consistently) is real but probably unimportant. A `WEAK SIGNAL` may be important but is
not yet proven. A verdict says the groups differ; it does not say *why*.

## Methods and assumptions

Everything is implemented in the standard library (`server/src/aobs_server/diagnosis/stats.py`)
and checked against brute-force enumeration and textbook values.

- **Numbers** (durations, tokens, cost, counts per run): the Mann–Whitney U / Wilcoxon rank-sum test.
  - Two-sided.
  - The p-value is **exact** (the full permutation distribution, ties handled with mid-ranks) up to
    60 runs in total; above that it uses the normal approximation with tie and continuity correction.
  - Based on ranks, so one extreme run moves it very little, and it assumes no particular
    distribution.
  - The effect size is the **Hodges–Lehmann** shift with its distribution-free confidence interval.
    The interval is exact for data without ties and approximate with ties.
- **Proportions** (failed runs, runs with repeated calls, an operation failing): **Fisher's exact test**,
  with **Wilson** 95 % intervals per group.
- **Multiple metrics:** testing many metrics at once raises the chance that one looks significant by
  luck. The **Holm–Bonferroni** correction controls the family-wise error rate.
  - There are two families: run-level metrics, and per-operation metrics (exploratory).
  - Constant metrics stay in the family. This is deliberately conservative.
  - A test runs 40 comparisons of groups drawn from the *same* distribution and checks that
    confident claims stay rare.
- **Outliers:** values beyond Q1 − 3·IQR or Q3 + 3·IQR (Tukey's far-out fences) are listed under
  *Data quality*. They are not removed; the rank test already limits their influence.
- **Assumptions:** runs are independent, and recorded under comparable conditions (same task,
  environment, inputs, load). A trend *within* a group (e.g. a warming cache) breaks independence.
  Different environments, services or SDK versions between the groups produce a warning.

## Minimum samples and insufficient data

At least **5 usable runs per group** (`--min-runs`, which can't be set below 3) are needed for a
test.

| Group sizes | Smallest possible two-sided p (complete separation) |
|---|---|
| 3 vs 3 | 0.1: can never be significant at 0.05 |
| 5 vs 5 | 0.008 |
| 10 vs 10 | 0.00001 |

After Holm, a 5-vs-5 result can still fall short. For example, 0.008 × 7 tests ≈ 0.056 is only a
weak signal, and the tests check this case. Aim for 10 or more runs per group.

The report says **insufficient data** instead of guessing when:
- a group has fewer than `--min-runs` finished runs (`few_runs.jsonl`);
- fewer than `--min-runs` runs report complete usage or cost (`partial_usage.jsonl`). The excluded
  runs are counted, never treated as zero;
- an operation is called in too few runs of one group.

Cost is not compared at all when currencies differ or no run reports cost on every call. Prices
are never estimated.

## Limitations

- **Statistical significance is not business impact.** Adjust the practical thresholds to what matters
  for your agent.
- **Dependent recordings fool the tests.** Runs recorded at the same moment against the same slow
  dependency are not independent; the result can look more certain than it is.
- **Operations are matched by name.** A renamed tool shows as one removed and one added operation;
  same-named calls in different places are grouped.
- **"Different task" detection checks only the run name and agent.** It cannot tell that the inputs
  differed.
- **Both directions are tested, but "worse" is assumed to mean "higher"** for every metric here (time,
  failures, retries, calls, tokens, cost).
