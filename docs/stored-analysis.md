# Analyzing traces stored in PostgreSQL

`analyze`, `compare`, `compare-groups`, `gate` and the dashboard can read the traces the collector
stored (`aobs-server serve`, [ingestion.md](ingestion.md)) as well as JSONL files. Wherever a
command takes a trace file, it also takes a **`db:` selection** of one project's stored traces:

```bash
uv run aobs-server analyze --project <project_id> db:trace=4bf92f3577b34da6a3ce929d0e0e4736
```

The analysis itself is unchanged. Stored traces go through the same loader as files: the privacy
step with a metadata-only ceiling, trace assembly, then the same rules, comparisons and policy
checks. So a stored run gets the same report as the file it was recorded from, and a test checks
that it does.

## Setup

You need a database the collector writes to. Follow [ingestion.md → Quick start](ingestion.md#quick-start):
PostgreSQL 16, `aobs-server db upgrade`, an organization, a project and an ingest key. Then send
traces with the SDK's `HttpExporter`. The analysis commands only need:

```bash
export AOBS_DATABASE_URL=postgresql://aobs:change-me@127.0.0.1:5432/aobs
```

and the project id that `aobs-server project create` printed.

To try it with the repository's sample recordings, post their batches to a running collector.
Each line of a JSONL file is one batch:

```bash
uv run aobs-server serve &                       # in another terminal, or in the background
while IFS= read -r batch; do
  curl -sS -X POST http://127.0.0.1:8000/v1/ingest -H "Authorization: Bearer $AOBS_KEY" \
    -H 'Content-Type: application/json' --data-binary "$batch" > /dev/null
done < examples/diagnose/compare/refund_outage.jsonl
```

Use a key without `--environment` for this, or one bound to `dev`, the samples' environment.

## Selecting stored traces

| Selection | Meaning |
|---|---|
| `db:trace=<id>` | one trace; repeat `trace=` or separate ids with commas for several |
| `db:task=answer_ticket` | runs whose root span is named `answer_ticket` |
| `db:agent=support_agent` | runs of this agent |
| `db:session=<id>` | runs of one session |
| `db:environment=prod` | runs that were sent from this environment |
| `db:status=error` | stored status: `ok`, `ok_with_errors`, `error`, `cancelled`, `incomplete`, `open` (not finished yet) |
| `db:since=2026-10-01` / `db:until=2026-10-02T12:00:00Z` | start time (ISO 8601; a date means midnight UTC; `until` is exclusive) |
| `db:limit=20` | how many, the most recently started first (default 100, at most 1000) |

Combine keys with `&` and quote the argument in the shell:
`'db:task=answer_ticket&status=error&limit=20'`. Values are URL-encoded: write a space as `%20` or
`+`.

**Find trace ids** with `traces list`. It takes the same selections (default: the 50 most recent
traces) and prints a `db:trace=<id>` input for each:

```bash
uv run aobs-server traces list --project <project_id>
uv run aobs-server traces list --project <project_id> 'db:task=answer_ticket&since=2026-10-01' --json
```

## Commands

```bash
P=<project_id>

# Diagnose one stored run, or every failed run of a task
uv run aobs-server analyze --project $P db:trace=<trace_id>
uv run aobs-server analyze --project $P 'db:task=answer_ticket&status=error&limit=20'

# A known-good stored run against later stored runs (one run per input; --current-trace picks one)
uv run aobs-server compare --project $P db:trace=<baseline_id> db:trace=<current_id>

# Last week's runs against today's runs of the same task
uv run aobs-server compare-groups --project $P \
  --baseline 'db:task=restock_check&since=2026-10-01&until=2026-10-08' \
  --current  'db:task=restock_check&since=2026-10-13'

# Reliability gate over today's stored runs; exit code 3 on FAIL
uv run aobs-server gate --project $P --policy examples/diagnose/gate/support_agent.toml \
  'db:task=answer_ticket&since=2026-10-13'
uv run aobs-server gate --project $P --policy examples/diagnose/gate/restock_baseline.toml \
  --baseline db:trace=<known_good_id> 'db:task=restock_check&since=2026-10-13'
```

Files and `db:` selections can be mixed, e.g. a baseline file against stored current runs. All
other options (`--json`, `--fail-on`, `--trace`, `--baseline-trace`, `--no-error-messages`, the
thresholds) work as they do for files.

## Dashboard

```bash
uv run aobs-server dashboard --project <project_id>
```

This adds a **Stored traces** dataset: the 200 most recently started traces of the project. It has
the overview, the run explorer, run details with diagnosis, and the two-run and group comparisons,
with trace ids where file datasets show file names. Like local files, it needs the dashboard token
printed at start-up. Newly stored traces appear on the next page load. If the database cannot be
read, the dashboard answers 503 for that dataset; the sample and local datasets keep working.

## Isolation and privacy

- **One project per command.** Every query filters on `--project`. Another project's trace id is
  answered exactly like an unknown id: `no stored trace <id> in project <project>`, exit code 1.
  Deleted (tombstoned) traces are never read.
- **The project's current privacy policy applies.** As the [read API](traces-api.md#privacy)
  does, stored events go through the project's privacy settings again. If you lower the capture
  ceiling or turn off error messages for a project, data stored earlier is hidden too. Then the
  analysis's own metadata-only step runs, as for files. Content is not even read: the
  `span_content` table is never queried. Error messages are secret-scrubbed and lose e-mail
  addresses and card numbers.
- **Who may run it.** These commands connect with `AOBS_DATABASE_URL`, like `project create` and
  `key create`. Whoever holds that URL can read every project. API keys and their scopes are not
  involved: they guard the HTTP API, not the operator's command line.

## Errors and exit codes

| Situation | Result |
|---|---|
| `db:` input without `--project`, `--project` without a `db:` input, malformed selection (bad trace id, unknown key, bad status, `limit` out of range, `since` ≥ `until`) | exit 2 with the reason |
| invalid or unknown project id, an explicit `trace=` id not stored in the project, database unreachable or not migrated | exit 1 with the reason, no traceback |
| a filter selection that matches nothing | reported as an input problem. `analyze` exits 1 (no traces). `compare` and `compare-groups` exit 1 (no runs). `gate` FAILs `gate.input_valid` and `gate.min_runs` (exit 3) |
| a stored trace that is still open, or was never finished | analyzed as in progress or incomplete. The gate's `require_complete` fails it |
| a trace truncated at ingest (the project's span limit) | reported as an input problem, so the gate fails `gate.input_valid` unless `on_input_problems = "warn"` |

## Limits

- **Memory.** A selection reads up to 1000 traces, each loaded in full (without content). Narrow
  it with `task`, `since` and `limit`.
- **Database time.** Each input is read in one query snapshot (`REPEATABLE READ`), so a trace
  written meanwhile is seen whole or not at all; `compare-groups` and `gate` read once per input.
- **Status.** `status=` filters on the status stored at ingest. `open` covers both "still
  running" and "never finished"; the report tells them apart using the time of the analysis.
- **Selection keys.** There is no filter by agent version or attribute yet: use explicit trace ids
  (`traces list` prints them) or time ranges.
- **Suggested commands.** A report's suggested next command names the `db:` input but not
  `--project`; add it when you run the command.
- **File names.** An input starting with `db:` is always a selection; give a file with such a
  name as `./db:…`.
- **No HTTP API yet.** Listing and analysis over HTTP (with read-scoped API keys) are not built;
  the dashboard and the CLI read the database directly.

## Tests

```bash
uv run pytest server/tests/test_stored_analysis.py   # real PostgreSQL 16 (throwaway cluster or AOBS_TEST_DATABASE_URL)
```

The tests ingest the SDK recordings in `examples/diagnose` through `POST /v1/ingest`. They check
that `analyze`, `compare`, `compare-groups`, `gate` and the dashboard give the same results for the
stored traces as for the files. They also cover:
- project isolation and deleted traces;
- the project's privacy policy, and that content is never shown;
- open and truncated traces;
- every usage error, and an unreachable database.
