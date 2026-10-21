# Reliability gate: `aobs-server gate`

`aobs-server gate` checks recorded agent runs against **requirements you write down** (a policy
file) and answers PASS or FAIL. On FAIL it exits with code 3, so a CI job stops. Every failed or
unknown check says why, with the spans of the trace that show it.

```bash
uv run aobs-server gate --policy examples/diagnose/gate/support_agent.toml examples/diagnose/compare/refund_ok.jsonl
uv run aobs-server gate --policy examples/diagnose/gate/support_agent.toml examples/diagnose/compare/refund_outage.jsonl   # FAIL, exit 3
```

It reads the same inputs as `analyze`: JSONL written by the SDK's `JsonlFileExporter`, JSON
batches, or, with `--project`, traces stored by the collector ([stored-analysis.md](stored-analysis.md)).
With files it needs no database, no server, no network access and no LLM, and it sends nothing
anywhere.

## Why a gate, when `analyze`, `compare` and `compare-groups` exist

Those commands report what *looks* wrong, judged by built-in heuristics, and `--fail-on` turns a
finding's severity into exit code 3. A gate turns **your** requirements into a verdict:

| | `analyze` / `compare` / `compare-groups` with `--fail-on` | `gate` |
|---|---|---|
| What decides failure | Built-in rules and their severity | Requirements in a versioned policy file |
| Thresholds | Heuristics (e.g. a span taking ≥ 50 % of the run) | Your limits: max duration, calls, retries, tokens, cost, required and forbidden tools, per-tool limits |
| Missing data (usage not reported, run never finished) | Reported as information | Every check is PASS, FAIL or UNKNOWN; the policy decides whether UNKNOWN fails |
| Several runs | One report per run, or statistics per group | Each run gets a verdict; the gate needs a minimum number of runs and a pass rate (e.g. 9 of 10) |
| Baseline | Lists every difference | Fails only on the differences you forbid (new error types, slower by more than N %, ...) |
| Output | Findings | A list of checks per run, each with expected value, actual value and evidence |

The gate does not replace those commands: it reuses their code (loading, privacy step, trace
assembly, run profiles, the comparison engine and the diagnosis rules), and its output points to
them for details.

## Design

### Inputs

```
aobs-server gate --policy POLICY.toml FILE... [--baseline FILE] [--baseline-trace ID] [--json] [--no-error-messages]
```

* `FILE...`: trace files, or `db:` selections of a project's stored traces with `--project`
  ([stored-analysis.md](stored-analysis.md)). Every run (trace) in them is checked, unless the
  policy's `task` limits the check to runs whose root span has that name.
* `--baseline FILE`: a known-good run for the `[baseline]` requirements. The file must hold one
  run, or `--baseline-trace` picks one by trace id prefix (as in `compare`).
* Loading is exactly what `analyze` does: strict JSON, schema validation, then the ingest
  privacy step with a metadata-only ceiling (content is never read or shown; error messages are
  secret-scrubbed; `--no-error-messages` drops them).

### Policy file (TOML)

TOML is read with the standard library (`tomllib`), so no dependency is added. Unknown keys and
wrong types are errors (exit code 2), so a typo like `max_tool_cals` cannot silently disable a
requirement. Every requirement is optional; a policy with no requirements is rejected.

```toml
version = 1                     # required; the only supported value

[gate]
task = "answer_ticket"          # optional: only runs whose root span has this name
min_runs = 1                    # at least this many runs must be checked (default 1)
min_pass_rate = 1.0             # share of runs that must pass, 0 to 1 (default 1.0 = every run)
on_missing_data = "fail"        # "fail" (default) or "warn": what an UNKNOWN check does to a run
on_input_problems = "fail"      # "fail" (default) or "warn": malformed lines, rejected events

[run]                           # checked on every run
require_success = true          # the run must end ok ...
allow_handled_errors = true     # ... and ok_with_errors (a child failed, the run recovered) counts as ok (default true)
require_complete = true         # no span without an end, no span whose parent is missing or cyclic
max_duration_ms = 30000         # the root span's duration
max_failed_spans = 0            # spans that ended with status error (the run's own root included)
max_tool_calls = 10
max_llm_calls = 8
max_retries = 2                 # spans that are retry attempts (retry.attempt > 1, or a retry_of link)
max_redundant_calls = 0         # successful calls repeated with identical input (same input fingerprint)
max_input_tokens = 5000         # summed over the run's model calls
max_output_tokens = 2000
max_cost = "0.05"               # provider-reported cost, as a decimal string
cost_currency = "USD"           # required with max_cost
required_tools = ["get_order_status"]
forbidden_tools = ["delete_account"]

[run.tools.issue_refund]        # limits for one tool (by tool name)
max_calls = 1
max_failures = 0

[baseline]                      # only with --baseline; each run is compared with the baseline run
forbid_outcome_regression = true    # baseline succeeded, run does not
forbid_new_error_types = true       # an operation fails with an error type the baseline did not have
forbid_new_tools = false            # a tool the baseline never called
forbid_removed_tools = true         # a tool the baseline called is no longer called
max_duration_increase_pct = 50      # run duration vs the baseline's ...
min_duration_increase_ms = 100      # ... fails only if it is also this many ms slower (default 100)
max_tool_call_increase = 2          # total tool calls, absolute
max_input_token_increase_pct = 25
```

A policy with a `[baseline]` section needs `--baseline`, and `--baseline` needs a `[baseline]`
section; either mismatch is a configuration error (exit code 2), so a CI job cannot silently skip
the baseline requirements.

**Timing noise.** Percentages of very short runs are mostly noise: the same healthy task recorded
twice can take 6 ms and then 59 ms (+883 %). `baseline.max_duration_increase_pct` therefore fails
only when the run is slower by more than that percentage **and** by more than
`min_duration_increase_ms` milliseconds (default 100, the same threshold `compare` uses for a run
slowdown; both limits inclusive). Set `min_duration_increase_ms = 0` for a pure percentage limit.
`min_duration_increase_ms` without `max_duration_increase_pct` is a configuration error.

### Check results: PASS, FAIL, UNKNOWN

Each requirement becomes one check per run, with a stable id (`run.max_tool_calls`,
`run.tools.issue_refund.max_calls`, `baseline.forbid_new_error_types`, ...), the expected value,
the actual value and evidence (span ids with what they show).

**UNKNOWN** means the trace does not contain what the check needs. It is never turned into PASS
silently:

| Situation | Check result |
|---|---|
| The run never finished (a span without an end) | `run.require_success`: UNKNOWN (no final status was recorded); `run.require_complete`: FAIL; duration checks: UNKNOWN; counts, tokens, cost and tool checks are lower bounds: FAIL if already above the limit (or a forbidden tool was called), otherwise UNKNOWN |
| A model call did not report usage | Token checks: UNKNOWN, unless the reported part alone already exceeds the limit (it is a lower bound), then FAIL |
| A model call did not report cost, or costs are in several currencies or another currency than `cost_currency` | `run.max_cost`: UNKNOWN |
| No model calls at all (in a finished run) | Token and cost checks: PASS with 0 |
| A tool or model call has no input fingerprint (fingerprinting off) | `run.max_redundant_calls`: UNKNOWN |
| Several root spans | duration checks: UNKNOWN |
| Baseline or current run unfinished | baseline duration and outcome checks: UNKNOWN |
| Baseline duration 0 ms | `baseline.max_duration_increase_pct`: PASS if the run is at most `min_duration_increase_ms` slower, otherwise UNKNOWN (a percentage of 0 is undefined) |

With `on_missing_data = "fail"` (the default) an UNKNOWN check fails its run. With `"warn"` it
does not, but it stays listed as UNKNOWN in the report.

### Verdict and exit codes

A run **passes** when it has no FAIL check (and no UNKNOWN check when `on_missing_data = "fail"`).
The gate **passes** when all of these hold:

* `gate.min_runs`: at least `min_runs` runs were checked;
* `gate.pass_rate`: passing runs / checked runs ≥ `min_pass_rate`;
* `gate.input_valid`: no input problems (malformed lines, rejected batches or events), unless
  `on_input_problems = "warn"`.

| Exit code | Meaning |
|---|---|
| 0 | PASS |
| 1 | A trace or baseline file does not exist, or the baseline file holds no runs |
| 2 | Invalid policy or options (unknown key, wrong type, `[baseline]` without `--baseline`, ambiguous baseline run) |
| 3 | FAIL |

These match the other commands (1 input, 2 options, 3 threshold reached). A trace file that holds
no runs (or none matching `task`) is not exit code 1: the gate FAILs on `gate.min_runs`, with the
reason in the report. A trace file that exists but cannot be read, and malformed lines, are input
problems (`gate.input_valid`).

### Output

Text by default; `--json` for machines. The JSON has `verdict`, `gate_checks`, and per run its
`checks` (each with `id`, `result`, `expected`, `actual`, `message`, `evidence`, `span_ids`).
Order is deterministic (runs by source file, start time and trace id; checks in policy order),
so two runs of the gate on the same files give the same output.

Two report files for CI can be written as well; stdout and the exit code are unchanged:

* `--junit FILE`: **JUnit XML**, the test-report format GitLab, Jenkins, Azure DevOps, CircleCI
  and Buildkite show natively (GitHub through a test-report action).
  - One test suite holds the gate-level checks; then one suite per checked run, with the trace id,
    run name, status and input as properties.
  - Each check is one test case. A failure's text holds the message, expected and actual values,
    evidence and span ids.
* `--markdown FILE`: a **Markdown summary**: the verdict, a table of failed checks (expected,
  actual, why), the runs, and what was not counted. Write it to `"$GITHUB_STEP_SUMMARY"` to show it
  on the GitHub Actions run page, or post it as a pull-request comment.

Both reports are red exactly when the gate fails (exit code 3):

| Check result | Reported as |
|---|---|
| PASS | passed |
| FAIL or UNKNOWN of a gate-level check | failed |
| FAIL of a run check (and UNKNOWN under `on_missing_data = "fail"`) | failed, or **skipped** with the reason when the gate still passed because `min_pass_rate` tolerates some failed runs |
| UNKNOWN under `on_missing_data = "warn"` | skipped, with the reason |
| WARN (`on_input_problems = "warn"`) | passed, with the warning attached |

Trace data is escaped: characters XML cannot hold are replaced, and Markdown gets no markup,
links, HTML or line breaks from trace names. Like the other outputs, the reports never contain
content, and error messages are secret-scrubbed. The directory of a report file is created if
needed; a file that cannot be written is exit code 1.

## In CI

**GitHub Actions:** [`examples/ci/github-actions-agent-gate.yml`](../examples/ci/github-actions-agent-gate.yml)
is a complete workflow to copy to `.github/workflows/`. It records runs, gates them, writes the
job summary and keeps the JUnit report and traces as an artifact. Its core:

```yaml
- name: Record agent runs (scripted mock model; no API key)
  run: |
    mkdir -p traces
    uv run python examples/support_agent/run.py --scenario success --out traces/success.jsonl --quiet
    uv run python examples/support_agent/run.py --scenario refund --out traces/refund.jsonl --quiet
- name: Reliability gate
  run: >-
    uv run aobs-server gate --policy examples/diagnose/gate/support_agent.toml traces/*.jsonl
    --junit reports/agent-gate.xml --markdown "$GITHUB_STEP_SUMMARY"
```

**GitLab CI** shows the JUnit report in merge requests:

```yaml
agent-gate:
  image: python:3.12
  script:
    - pip install uv && uv sync --locked
    - mkdir -p traces && uv run python examples/support_agent/run.py --scenario refund --out traces/refund.jsonl --quiet
    - uv run aobs-server gate --policy agent-gate.toml traces/*.jsonl --junit agent-gate.xml --markdown agent-gate.md
  artifacts:
    when: always
    reports:
      junit: agent-gate.xml
    paths: [agent-gate.md]
```

The job fails when the gate exits non-zero. Recording the runs (here: the example agent with its
scripted mock model) is your test harness's job; the gate only judges what was recorded. To gate
runs the collector stored instead of files, pass `db:` selections with `--project`
([stored-analysis.md](stored-analysis.md)), e.g. `'db:task=answer_ticket&since=2026-10-13'`.

`server/tests/test_gate_reports.py` runs the template's commands. Neither the workflow as a
whole (GitHub's runners and actions) nor the GitLab job has been run from this repository.

## Limits

* It judges what the trace records. Anything the SDK does not record (content, which is never
  read; the link between a model's tool call and the tool span) cannot be checked.
* `max_redundant_calls` relies on input fingerprints, which are comparable only within one
  process unless a stable fingerprint key is configured; arguments that are objects without a
  stable representation can make fingerprints unreliable (see the SDK documentation).
* Cost checks use only provider-reported cost; the gate does not estimate cost from tokens.
* Baseline checks compare against **one** run. A single baseline run is a single sample: use
  `compare-groups` to judge whether a difference is beyond normal run-to-run variation, and keep
  baseline limits generous.
* JUnit test cases have no duration (`time="0"`): a check is a judgement over recorded data, not
  a timed test.
