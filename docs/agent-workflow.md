# End-to-end workflow with the example agent

This walkthrough runs a small tool-calling support agent, records what it does with the aobs
SDK, sends the telemetry to the collector, stores it in PostgreSQL, and then lists, analyzes,
compares and gates the stored runs. At the end you look at the same runs in the dashboard.
It needs no model API key and no internet access: a scripted mock model stands in for the LLM.

```
examples/support_agent/run.py ──SDK (HttpExporter)──▶ aobs-server serve ──▶ PostgreSQL
                                                                              │
              aobs-server traces list / analyze / compare / gate / dashboard ◀┘
```

Every command below was run in this order, and the outputs shown are from those runs. The
exception is the `docker` commands: Docker was not available where these docs were checked, so
a local PostgreSQL 16 stood in for the container. `server/tests/test_agent_workflow.py`
runs the same steps automatically (see [Automated check](#automated-check)).

## 1. The agent

`examples/support_agent/` holds a support agent with two tools:

| File | What it does |
|---|---|
| `agent.py` | The agent loop. It asks the model; when the model asks for a tool (`get_order_status`, `issue_refund`), it runs the tool and asks the model again, until the model answers in text. |
| `mock_llm.py` | A scripted model behind the real `openai` client (through its HTTP mock transport). It is deterministic, needs no key and makes no network call. |
| `run.py` | Starts the SDK, picks the scenario, runs the agent once, and flushes the telemetry to a file or to the collector. |

Instrumentation is ten statements, each marked `# aobs`:

- `aobs.run("answer_ticket", …)` makes the **agent span**, the root of the trace. It holds the
  agent name and version, and the ticket id as the session id.
- `openai_compat.wrap(client, …)` makes an **llm span** for every model call, with the model
  names, token usage, timing, and errors (type, HTTP status, retryable).
- `@aobs.tool` makes a **tool span** per call, with timing, a keyed fingerprint of the
  arguments, and the error if it raised.
- Spans nest automatically: every llm and tool span is a child of the agent span. An exception
  that escapes a tool or a model call marks that span *and* the run as failed.
- `aobs.init(…)` and `aobs.shutdown()` configure and flush the SDK.

Scenarios (`--scenario`):

| Scenario | What happens | Run status |
|---|---|---|
| `success` | looks up order 1042 and answers | ok |
| `refund` | looks it up, issues a refund, answers | ok |
| `refund-outage` | the refund tool raises `TimeoutError` (a simulated payments outage) | error |
| `model-outage` | the model call fails with HTTP 503 (a simulated provider outage); no tool runs | error |

`--no-usage` makes the mock model leave out token usage, as some providers do. `--capture`
chooses what content the SDK sends: `metadata_only` (default), `redacted` or `full`.

**Each run answers one ticket.** `--ticket` names it (default: a fresh `ticket-<scenario>-<id>`).
The ticket id is stored as the run's session id, so `db:session=<ticket>` selects exactly that
run later.

### Simulated or real

| Part | In this walkthrough | With a real provider |
|---|---|---|
| Model calls | **Simulated** by `mock_llm.py`. Recorded with provider `mock_llm` and model `mock-model`, never as a real provider. | `--mode live` with `OPENAI_API_KEY` and `AOBS_EXAMPLE_MODEL` (any OpenAI-compatible API). **Not run by the test suite and not verified here.** |
| Token usage | Made up by the mock model, roughly from the message length | Reported by the provider, when it reports it |
| Cost | Not reported (the mock model has no price) | Only if the provider reports it; aobs never estimates cost |
| Tools, the payments outage | Simulated in `agent.py` (`RefundService.fail`) | Your own tools |
| SDK, collector, PostgreSQL, CLI, dashboard | **Real**: the same code paths as in production | the same |

## 2. Set up and start the backend

**Prerequisites:** Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and a PostgreSQL 16
server. From the repository root:

```bash
uv sync
```

**PostgreSQL 16** with Docker (or use any PostgreSQL 16 you can create a database on):

```bash
docker run -d --name aobs-pg -e POSTGRES_USER=aobs -e POSTGRES_PASSWORD=change-me \
  -e POSTGRES_DB=aobs -p 127.0.0.1:5432:5432 postgres:16
export AOBS_DATABASE_URL=postgresql://aobs:change-me@127.0.0.1:5432/aobs
```

`change-me` is a local development password; use your own. `uv run aobs-server config` prints the
settings with the password masked. If Docker answers that the name `aobs-pg` is already in use,
the container exists from an earlier session: start it with `docker start aobs-pg`.

**Schema, organization, project and API key.** The commands print JSON. These lines keep the ids
and the key in shell variables, so the key is not printed to your terminal:

```bash
uv run aobs-server db upgrade
ORG=$(uv run aobs-server org create --name demo | uv run python -c 'import json,sys; print(json.load(sys.stdin)["organization_id"])')
PROJECT=$(uv run aobs-server project create --org "$ORG" --name support-agent \
  | uv run python -c 'import json,sys; print(json.load(sys.stdin)["project_id"])')
export AOBS_API_KEY=$(uv run aobs-server key create --project "$PROJECT" --name example-agent --environment dev \
  | uv run python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
echo "project: $PROJECT"
```

- The project stores **metadata only** (`project create`'s default `--capture-ceiling`): even if an
  SDK sends prompts or tool arguments, the collector drops them before storing.
- The key can write only to the `dev` environment, the one the example agent uses. Only its hash
  is stored, and it cannot be shown again: keep the variable, or store the key in your secret
  manager.

Run every other command of this page **in this terminal**: it holds `PROJECT` and
`AOBS_API_KEY`. A new terminal starts without them. If you lose them, run the `ORG`/`PROJECT`/
`AOBS_API_KEY` lines again: you get a new project, and the old runs stay in the old one.

**Start the collector** in a second terminal. It needs the database URL too, so set it there again:

```bash
export AOBS_DATABASE_URL=postgresql://aobs:change-me@127.0.0.1:5432/aobs
uv run aobs-server serve        # http://127.0.0.1:8000; Ctrl+C stops it
```

## 3. Run the agent and send its runs to the collector

```bash
uv run python examples/support_agent/run.py --scenario refund        --ticket demo-ok          --endpoint http://127.0.0.1:8000
uv run python examples/support_agent/run.py --scenario refund-outage --ticket demo-refund-down --endpoint http://127.0.0.1:8000
uv run python examples/support_agent/run.py --scenario model-outage  --ticket demo-model-down  --endpoint http://127.0.0.1:8000
uv run python examples/support_agent/run.py --scenario refund --no-usage --ticket demo-no-usage --endpoint http://127.0.0.1:8000
```

Output of the first two:

```
agent: Your refund rf_1042 was issued.
sent the run for ticket demo-ok to http://127.0.0.1:8000
  find it: uv run aobs-server traces list --project <project id> 'db:session=demo-ok'

agent failed: TimeoutError: payments API did not respond within 2s
sent the run for ticket demo-refund-down to http://127.0.0.1:8000
  find it: uv run aobs-server traces list --project <project id> 'db:session=demo-refund-down'
```

- The failing scenarios exit with code 1, like a real agent process whose run failed. Their
  telemetry is still sent.
- If the collector is not running, or the key is wrong, `run.py` prints
  `warning: not all telemetry was exported`.

**Running the commands again** adds another run to the same ticket. Then
`db:session=demo-ok` selects both runs, and `compare` asks you to pick one. Use new ticket
names, or add `&limit=1` (the most recent run): `'db:session=demo-ok&limit=1'`.

## 4. Inspect the stored runs

```bash
uv run aobs-server traces list --project "$PROJECT"
```

```
TRACE ID                          STARTED (UTC)         STATUS          SPANS  ERRORS  RUN / AGENT
c590a7e5d19455e233fc842d00c1e4c4  2026-10-15 05:31:42   ok                  6       0  answer_ticket / support_agent
4e6b38b30100202d9f82b80c366cd6f7  2026-10-15 05:31:41   error               2       2  answer_ticket / support_agent
d99947f2a93b55b06c420c680b9ff907  2026-10-15 05:31:40   error               5       2  answer_ticket / support_agent
715e875831209e529ab0d732c29f56c0  2026-10-15 05:31:39   ok                  6       0  answer_ticket / support_agent
4 trace(s); analyze one with: aobs-server analyze --project <project id> db:trace=<TRACE ID>
```

The error counts include the agent span: a failed tool or model call fails the run too.

## 5. Analyze a successful run and a failed run

```bash
uv run aobs-server analyze --project "$PROJECT" db:session=demo-ok
uv run aobs-server analyze --project "$PROJECT" db:session=demo-refund-down
```

The failed run's report shows the span tree and names the root cause with evidence:

```
Trace d99947f2a93b55b06c420c680b9ff907  run 'answer_ticket'  status=ERROR
  5 spans (1 agent, 2 llm, 2 tool), 2 failed, 0 unfinished, duration 52.5 ms
  service support-agent-example (dev), aobs-python 0.1.0.dev0, source capture mode metadata_only

Execution
  agent answer_ticket                                       52.5 ms  ERROR       TimeoutError  [d1cbe20d]
    +- llm chat (mock-model-2026-01-01)                     15.1 ms  ok          tokens 51/12  [14212355]
    +- tool get_order_status                                 0.1 ms  ok            [4f0c87ce]
    +- llm chat (mock-model-2026-01-01)                      2.7 ms  ok          tokens 70/12  [e4b80ed3]
    +- tool issue_refund                                     0.2 ms  ERROR       TimeoutError  [4fdbc579]

  1. [ERROR] tool 'issue_refund' failed with TimeoutError   (error.root_cause)
     Evidence:
       - span 4fdbc579 (tool issue_refund) ended with status error after 0.2 ms
       - error type TimeoutError; origin=timeout
       - message (secret-scrubbed): payments API did not respond within 2s
       - parents that also failed: agent answer_ticket (TimeoutError)
     Suggestion: The call timed out. Check the downstream service's health and latency at that time, …
```

(The span ids and durations differ on every run.) Content is never shown: the report has no
prompts, answers or tool arguments, and error messages are secret-scrubbed.

## 6. Compare the good run with the failed one

```bash
uv run aobs-server compare --project "$PROJECT" db:session=demo-ok db:session=demo-refund-down
```

```
  1. [ERROR] the run succeeded in the baseline and now ends with status error   (regression.run_failed)
     Measured: baseline ok  ->  current error   [outcome changed from success to failure]
     Evidence:
       - current root cause: tool 'issue_refund' failed with TimeoutError
  2. [ERROR] new error: tool 'issue_refund' failed with TimeoutError   (regression.new_error)
     Measured: baseline no such error  ->  current 1 failure(s) with TimeoutError   [+1 failure(s)]
     Evidence:
       - current spans 4fdbc579
       - baseline: 0 failure(s) in 1 call(s)
```

## 7. Run the reliability gate

`examples/diagnose/gate/support_agent.toml` is the policy for this agent. The run must:
- succeed and be complete;
- stay within 10 s, 6 model calls and 4 tool calls;
- make no retries and no redundant calls, and use at most 4,000 input tokens;
- call `get_order_status`;
- call `issue_refund` at most once, and without a failure.

Missing data counts as a failure.

```bash
uv run aobs-server gate --project "$PROJECT" --policy examples/diagnose/gate/support_agent.toml db:session=demo-ok
echo "exit code: $?"      # 0: PASS
uv run aobs-server gate --project "$PROJECT" --policy examples/diagnose/gate/support_agent.toml db:session=demo-refund-down
echo "exit code: $?"      # 3: FAIL
```

The failing run is rejected for the refund failure, with the span that shows it:

```
aobs reliability gate: FAIL
  FAIL     gate.pass_rate: 1 of 1 run(s) failed  [expected at least 100% of runs pass, actual 0 of 1 (0%)]
             - run d99947f2a93b 'answer_ticket' in db:session=demo-refund-down
  FAIL     run.require_success: the run ended with status error  [expected ok or ok_with_errors, actual error]
             - root cause: tool 'issue_refund' failed with TimeoutError; span 4fdbc579 (tool issue_refund) ended with status error after 0.2 ms
  FAIL     run.tools.issue_refund.max_failures: 1 failure(s) of 'issue_refund', limit 0  [expected at most 0, actual 1]
             - span 4fdbc579 (tool issue_refund) ended with status error (TimeoutError)
```

Every other check of that run passes, so the report points at the one thing that went wrong
(`gate.pass_rate` fails because this run, the only one checked, failed).

**CI reports.** The same command can also write a JUnit XML report (one test case per check, for
CI test-report views) and a Markdown summary. They change neither the output nor the exit code:

```bash
uv run aobs-server gate --project "$PROJECT" --policy examples/diagnose/gate/support_agent.toml db:session=demo-refund-down \
  --junit /tmp/aobs-reports/gate.xml --markdown /tmp/aobs-reports/gate.md
cat /tmp/aobs-reports/gate.md
```

```
### aobs reliability gate: **FAIL**

0 of 1 run(s) passed · policy `examples/diagnose/gate/support_agent.toml` · missing data: fail · input problems: fail · task: answer\_ticket

#### Failed checks

| Where | Check | Expected | Actual | Why |
|---|---|---|---|---|
| gate | `gate.pass_rate` | at least 100% of runs pass | 0 of 1 (0%) | 1 of 1 run(s) failed; run d99947f2a93b 'answer\_ticket' in db:session=demo-refund-down |
| run `d99947f2a93b` answer\_ticket | `run.require_success` | ok or ok\_with\_errors | error | the run ended with status error; root cause: tool 'issue\_refund' failed with TimeoutError; span 4fdbc579 (tool issue\_refund) ended with status error after 0.2 ms |
| run `d99947f2a93b` answer\_ticket | `run.tools.issue_refund.max_failures` | at most 0 | 1 | 1 failure(s) of 'issue\_refund', limit 0; span 4fdbc579 (tool issue\_refund) ended with status error (TimeoutError) |

#### Runs
…
```

In GitHub Actions, write the Markdown to `"$GITHUB_STEP_SUMMARY"` to show it on the run page
([reliability-gate.md → In CI](reliability-gate.md#in-ci); a workflow template is in
`examples/ci/`).

## 8. Reproduce a failure and read its report

| Run | Gate result | Why |
|---|---|---|
| `demo-refund-down` | FAIL | `run.require_success` (root cause: `issue_refund` `TimeoutError`) and `run.tools.issue_refund.max_failures` |
| `demo-model-down` | FAIL | `run.require_success` (root cause: llm `chat` failed with `openai.InternalServerError`, HTTP 503, retryable); `run.required_tools.get_order_status` (never called); `run.max_input_tokens` UNKNOWN (the failed call reported no usage) |
| `demo-no-usage` | FAIL | `run.max_input_tokens` is **UNKNOWN**: the run succeeded but reported no token usage. The policy says `on_missing_data = "fail"`, so missing data fails rather than passing silently. |

```bash
uv run aobs-server gate --project "$PROJECT" --policy examples/diagnose/gate/support_agent.toml db:session=demo-model-down
uv run aobs-server analyze --project "$PROJECT" db:session=demo-model-down
```

How to read a gate report:
- Each line is one check: its result, what it measured, and `[expected …, actual …]`.
- The indented lines below a FAIL are its evidence: the failing span (id prefix, kind and name),
  the error type, and the root cause.
- `aobs-server analyze` on the same selection shows the full span tree and a suggestion.
- `gate.pass_rate` fails whenever a run fails, unless the policy's `min_pass_rate` allows some
  failures.

## 9. See the runs in the dashboard

```bash
uv run aobs-server dashboard --no-samples --project "$PROJECT"
```

- Open the printed link, which ends in `#token=…`. The token protects stored traces. In a
  Codespace, open port **8800** from the *Ports* tab, then add the `#token=…` part of the printed
  link to the end of the address.
- The **Stored traces** dataset shows the runs: an overview, the run list (filter *failed*), and
  run details with the span timeline and root cause. *Compare* works with two runs or two groups
  of runs.
- Leave out `--no-samples` to keep the sample datasets as well. See [dashboard.md](dashboard.md).

## Clean up

```bash
# Ctrl+C in the terminals running `serve` and `dashboard`, then:
uv run aobs-server key list --project "$PROJECT"     # the key's "id" (the key itself is never shown)
uv run aobs-server key revoke --key-id <id>
docker rm -f aobs-pg                                  # removes the container and every stored run
rm -rf /tmp/aobs-reports
```

## Privacy and secrets

- The project stores metadata only. A run made with `--capture full` sends prompts and tool
  arguments, and the collector drops them before storing; the automated check verifies this.
- Error messages are secret-scrubbed. Tool arguments are stored only as keyed fingerprints, which
  is enough to detect repeated calls.
- The API key is read from `AOBS_API_KEY` and never printed by `run.py`. The collector stores only
  its hash.
- The ticket id is stored as the run's session id as it is (ids and names are metadata, not
  redacted content): use an opaque ticket number, never a customer's name or e-mail address.

## Connecting a real model provider

`--mode live` sends the same agent to any OpenAI-compatible API:

```bash
AOBS_EXAMPLE_MODE=live OPENAI_API_KEY=… AOBS_EXAMPLE_MODEL=<model> \
  uv run python examples/support_agent/run.py --scenario refund --endpoint http://127.0.0.1:8000
```

Nothing else changes: the same `openai_compat.wrap` records the calls, with the provider and model
names the API reports. This path has **not** been run by the test suite or for this walkthrough.
`model-outage` is mock-only. To instrument your own agent, see [sdk.md](sdk.md).

## Automated check

```bash
uv run pytest server/tests/test_agent_workflow.py      # needs PostgreSQL 16 (a throwaway cluster, or AOBS_TEST_DATABASE_URL)
```

It does what this page does, without Docker:
1. Starts the collector on a local port, against a fresh PostgreSQL database.
2. Runs `run.py` for each scenario as a separate process, sending with `--endpoint`, plus one
   `--capture full` run.
3. Checks that each run is stored once, with its status, and that the spans are connected.
4. Checks the tool and model errors and the `mock_llm` labelling.
5. Runs analysis, comparison and the gate (including JUnit output), and the missing-data rule.
6. Checks that neither content nor the API key is stored or printed, and that the dashboard shows
   the runs.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `warning: not all telemetry was exported` | The collector is not running at `--endpoint`, `AOBS_API_KEY` is wrong or revoked, or the key is bound to another environment |
| `aobs-server: db: inputs need --project PROJECT_ID` | Add `--project "$PROJECT"` |
| `… holds 2 runs; choose one with a trace id prefix` | The ticket was used twice: add `&limit=1`, or use `--current-trace` / `--baseline-trace` |
| `cannot read the database (OperationalError)` | `AOBS_DATABASE_URL` is unset or PostgreSQL is not running |
| Requests to `127.0.0.1` fail behind a corporate proxy | Set `NO_PROXY=127.0.0.1,localhost` |
