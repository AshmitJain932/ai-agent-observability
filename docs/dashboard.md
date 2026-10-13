# AOBS dashboard (first version)

A read-only web dashboard for agent traces. It shows:
- an overview of runs: success and failure, duration, tokens, provider-reported cost;
- a run explorer with filters;
- run details: execution timeline, failed operations, diagnosis findings with evidence and next steps;
- regression analysis for two runs or two groups of runs.

It reuses the same code as `aobs-server analyze`, `compare` and `compare-groups`, so the dashboard
and the CLI always agree.

**What it reads:** saved JSONL trace files, the format the SDK's `JsonlFileExporter` writes.
- **Sample data (default):** the recordings in `examples/diagnose/`. These are healthy, slow,
  flaky, refund-outage, retry-storm and crashed runs, recorded with the real SDK, and are clearly
  labelled *Sample data* in the dashboard.
- **Local files:** your own trace files, from a directory you name with `--data-dir`.

**What it does not read:** the collector database. The collector cannot receive and store runs yet;
see *Not implemented yet* below.

## Quick start (GitHub Codespaces or any Linux/macOS machine)

**Prerequisites:** Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/). In a Codespace, if
`uv --version` fails, install it with:

```bash
pip install uv
```

**Install and start**, from the repository root:

```bash
uv sync
uv run aobs-server dashboard
```

The terminal prints `aobs dashboard on http://localhost:8800/`. To open it:

- **In a Codespace:** open the **Ports** tab, next to *Terminal*. Find port **8800**, which is
  forwarded automatically, and click the globe icon (*Open in Browser*). VS Code may also pop up
  *"Your application running on port 8800 is available"*: click **Open in Browser**.
  - Leave the port's visibility **Private**, the default. Only you, signed in to GitHub, can open it.
- **On your own machine:** open <http://localhost:8800/>.

**Stop it** with **Ctrl+C** in the terminal. Use another port with `--port 8900`.

## Using the dashboard

Pick a **dataset** at the top right. Three sample datasets are built in:

| Dataset | What it holds | Good for |
|---|---|---|
| Sample: repeated runs (inventory agent) | 63 runs: 10 healthy, 10 healthy again, 10 slow, 10 flaky, 10 with one cold start, 3 slow, 10 with incomplete usage | Overview, runs explorer, **Compare → Groups of runs** |
| Sample: before/after runs | Single runs before and after a change: refund OK vs refund outage, inventory v1 vs v2 | **Compare → Two runs** |
| Sample: single-run failures | Refund outage, retry storm, repeated calls with a slow search, a crashed run, a healthy run | **Runs → run details** (diagnosis) |

- **Overview:**
  - total, succeeded and failed runs, and the failure rate. Failed ÷ finished runs; unfinished and
    cancelled runs are listed but not counted as failures;
  - median run duration;
  - token and cost totals. Each tile says how many runs the sum covers. Runs that didn't report
    usage or cost on every model call are **left out, never counted as zero**. Cost is only ever
    what the provider reported; prices are never estimated;
  - recent runs.
- **Runs:**
  - every run, with status, task, agent, file, start time, duration, failed spans and diagnosis
    findings;
  - filter by status and task;
  - click a run name to open its details.
- **Run details:**
  - the diagnosis findings, each with evidence and a suggested next step;
  - the execution tree with a timeline bar per operation (failed operations in red, with their
    error type);
  - tool-call totals;
  - tokens and provider-reported cost, shown as *not reported* when absent.
- **Compare:**
  - **Two runs**, the same as `aobs-server compare`: pick a baseline and a current file, plus a
    run if a file holds several. Each finding shows what was measured, the evidence, why it
    matters, possible explanations (marked *not verified*) and a next step;
  - **Groups of runs**, the same as `aobs-server compare-groups`: tick the baseline files and the
    current files. Every metric shows medians, sample sizes, the Holm-adjusted p-value and a
    verdict, and says *insufficient data* when there are too few runs;
  - sample datasets have one-click buttons for ready-made comparisons. Each button's tooltip says
    what the recording simulates.

## Your own traces (local files mode)

1. Record runs into a directory. The example agent can do this:

   ```bash
   mkdir -p my_traces
   uv run python examples/support_agent/run.py --scenario refund --out my_traces/refund_ok.jsonl --quiet
   uv run python examples/support_agent/run.py --scenario refund-outage --out my_traces/refund_outage.jsonl --quiet
   ```

   In your own agent, use `aobs.init(..., exporter=JsonlFileExporter("my_traces/run.jsonl"))`.

2. Start the dashboard on that directory:

   ```bash
   uv run aobs-server dashboard --data-dir my_traces
   ```

   The terminal prints a **token** and a link ending in `#token=…`. Local files are private, so the
   dashboard asks for this token before showing them. Either:
   - open the printed link. In a Codespace, open port 8800 from the Ports tab, then add
     `#token=…` to the end of the address; or
   - choose *Local trace files* in the dataset menu and paste the token when asked.

   The token lasts until the dashboard stops, and a new one is generated on every start. To fix one
   yourself, set `AOBS_DASHBOARD_TOKEN`. The sample datasets stay available alongside your files;
   hide them with `--no-samples`.

New or changed files are picked up on the next page load; no restart is needed. Malformed lines
are listed as *input problems*, and the rest of the file is still used.

## Security and privacy

- **Content is never shown.** Every file goes through the collector's privacy step with a
  metadata-only ceiling, so prompts, model responses, tool arguments and tool results are dropped
  before anything reaches the page. Error messages and attributes are secret-scrubbed: API keys,
  bearer tokens, passwords and credentials in URLs are replaced with `[REDACTED:…]`.
- **Read-only.** The API has only `GET` routes. It reads files only from the dataset directories, by
  the names the server lists. Paths sent by the browser are rejected, and symbolic links are
  ignored.
- **Local files need the token.** It is compared in constant time, and is never stored by the server
  or written to the address bar after use.
- **No tenant data.** The dashboard does not load the database or the API-key layer at all, which a
  test checks, so it cannot show another project's runs.
- **Browser hardening.**
  - A strict Content-Security-Policy: scripts only from the dashboard itself, no inline scripts or
    styles, no `eval`.
  - Every trace value is inserted as text, never as HTML, so a span named `<img onerror=…>` shows up
    as text. A test checks this.
- **Network exposure.** The dashboard listens on `127.0.0.1` by default. `--host 0.0.0.0` makes it
  reachable from other machines and prints a warning. Keep Codespaces ports *Private*.

## Tests

```bash
uv run pytest server/tests/test_dashboard_api.py      # API: numbers, filters, errors, privacy, token, files
uv run pytest server/tests/test_dashboard_ui.py       # real browser: loading/empty/error states, navigation, XSS
```

The browser tests need Chromium for Playwright. In a fresh Codespace, run this once:

```bash
uv run playwright install --with-deps chromium
```

Without a browser they are skipped with that instruction; the API tests still run.

## What works and what is not implemented yet

**Works now:**
- all four views over sample data and over local JSONL files;
- loading, empty and error states;
- the token-protected local mode.

**Not implemented yet:**
- **Live data from the collector.** There is no `POST /v1/ingest`, and nothing stores runs in
  PostgreSQL yet. When that exists, the dashboard should read stored runs through
  project-scoped, API-key-authenticated endpoints. The diagnosis rules already work on assembled
  traces, so they can run on stored data unchanged.
- **User accounts and logins:** a token, not users.
- Dashboards across many projects.
- Alerting, charts over time, and cost estimates from price tables.

**Limits:**
- Every request reloads files that changed, so very large directories are slow. At most 200 files,
  each up to 50 MiB, are read.
- Time values are shown in UTC.
