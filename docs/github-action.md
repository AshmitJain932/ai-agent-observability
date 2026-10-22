# GitHub Action: the reliability gate as one step

[`actions/gate`](../actions/gate/action.yml) runs [`aobs-server gate`](reliability-gate.md) in a
GitHub Actions job: it checks the recorded agent runs against your policy, shows the result as the
job summary, writes a JUnit report, and can keep one comment on the pull request up to date. You
write one step instead of copying a workflow:

```yaml
- name: Reliability gate
  id: gate
  uses: AshmitJain932/ai-agent-observability/actions/gate@<commit-sha>
  with:
    policy: agent-gate.toml
    traces: traces/*.jsonl
```

Recording the runs (any code that runs your agent with the SDK's `JsonlFileExporter`) stays your
test harness's job, in a step before this one. A complete workflow to copy:
[`examples/ci/github-actions-agent-gate-action.yml`](../examples/ci/github-actions-agent-gate-action.yml).
The same job with plain commands instead of the action:
[`examples/ci/github-actions-agent-gate.yml`](../examples/ci/github-actions-agent-gate.yml).

## What the step does

1. Sets up Python (`actions/setup-python`, with `update-environment: false`, so the job's `PATH`
   and the Python of your other steps do not change).
2. Installs `aobs-server` into a private virtual environment under `RUNNER_TEMP`. By default it
   installs the `server/` package **of the same checkout as the action** (`github.action_path`),
   so the gate is always the version of the action you pinned. aobs-server is not on PyPI yet;
   once it is, `install-source: aobs-server==X.Y` installs a release instead. pip resolves the
   dependencies within the ranges in `server/pyproject.toml` (not from `uv.lock`), so the runner
   needs access to a package index.
3. Runs `aobs-server gate --policy POLICY --junit … --markdown … -- TRACES…`. The text report goes
   to the step log.
4. Appends the Markdown report to `$GITHUB_STEP_SUMMARY`, so it shows on the run page.
5. With `comment-on-pr: "true"` on a pull request: posts the same report as a comment, or edits the
   comment it posted before (one comment per pull request, found again by a hidden marker).
6. Passes or fails the step: PASS passes; FAIL fails (exit code 3) unless
   `fail-on-regression: "false"`; no verdict (a missing file, an invalid policy) always fails, with
   the gate's exit code.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `policy` | (required) | The gate policy file (TOML; [reliability-gate.md](reliability-gate.md)) |
| `traces` | (required) | Runs to check, **one per line**: a trace file, a glob pattern (`traces/*.jsonl`; `**` matches directories) or a `db:` selection. A pattern that matches nothing is passed on, so the gate reports the file missing. Spaces in a line belong to the file name |
| `baseline` | | Known-good run for the policy's `[baseline]` requirements (file or `db:` selection) |
| `baseline-trace` | | Pick the baseline run by trace id prefix |
| `project` | | Project id for `db:` selections ([stored-analysis.md](stored-analysis.md)); set `AOBS_DATABASE_URL` in the step's `env` from a secret |
| `no-error-messages` | `"false"` | Leave error messages out of every report |
| `report-dir` | new directory under `RUNNER_TEMP` | Where `agent-gate.xml` and `agent-gate.md` are written |
| `python-version` | `"3.13"` | Python for aobs-server (3.11 or later) |
| `install-source` | this action's `server/` | What pip installs as aobs-server (a path, a requirement such as `aobs-server==X.Y`, or a VCS URL) |
| `comment-on-pr` | `"false"` | Post or update one pull-request comment with the report |
| `comment-tag` | `default` | Keeps the comments of several gate steps in one pull request apart (letters, digits, `.`, `_`, `-`) |
| `fail-on-regression` | `"true"` | `"true"` (default): a FAIL fails the step. `"false"`: report only; the step passes, with a notice |

Boolean inputs must be `"true"` or `"false"`; anything else fails the step as a configuration
error, so a typo cannot silently turn the gate off.

## Outputs

| Output | Value |
|---|---|
| `result` | `pass`, `fail`, or `error` (no verdict) |
| `exit-code` | Exit code of `aobs-server gate`: 0 pass, 1 missing input, 2 invalid policy or options, 3 fail |
| `junit-path` | The JUnit XML report, e.g. for `actions/upload-artifact` or a test-report action |
| `markdown-path` | The Markdown report (the same text as the job summary) |
| `comment-url` | The pull-request comment, when one was posted or updated |

To act on a FAIL yourself, set `fail-on-regression: "false"` and read `steps.<id>.outputs.result`.

## Permissions

The gate itself needs only `contents: read`. The pull-request comment needs
`pull-requests: write` for the job's `GITHUB_TOKEN`:

```yaml
permissions:
  contents: read
  pull-requests: write # only for comment-on-pr
```

* The comment is written with `gh api` (preinstalled on GitHub-hosted runners) and the job's
  default `GITHUB_TOKEN`, passed to `gh` in its environment only. The action never prints it.
* On `pull_request` events from a **fork**, GitHub gives the workflow a read-only token, so the
  action skips the comment with a notice, before any request. The job summary still shows the
  report.
* When the token cannot write (no `pull-requests: write`, a Dependabot pull request, a runner
  without `gh`), the comment step warns and the gate's verdict decides the step as usual: a missing
  comment never fails a job.
* Outside `pull_request` / `pull_request_target` events (`push`, `workflow_dispatch`) there is no
  comment, with a notice. The action does not recommend `pull_request_target` for this:
  that event runs with a write token, so check out and run untrusted pull-request code there only
  if you know the risks.
* Only a comment by `github-actions[bot]` (the author of comments written with `GITHUB_TOKEN`)
  that starts with the marker is edited; a comment someone else wrote with the marker is never
  touched.

## Untrusted trace data

Traces are recorded from your agent, which handles untrusted input, so the action treats trace
contents as untrusted:

* **No script injection.** Every input reaches the shell through an environment variable; no
  `${{ }}` expression is interpolated into a `run:` script. Trace inputs are passed after `--`, so
  a file name that starts with `-` is not read as an option, and globs never split names at spaces.
* **No workflow commands from traces.** The gate prints run names to the log; the action wraps its
  output in `::stop-commands::` with a random token, so a run named `::error::…` or
  `::add-mask::…` is plain text.
* **Markdown.** The job summary and the comment are the gate's own Markdown report: trace data in
  it gets no markup, links, HTML or line breaks (`<`, `>` and `&` become entities; `` \ ` * _ [ ] | # ~ ``
  are backslash-escaped; control characters and line breaks become spaces). The action adds only
  fixed text: a hidden marker, a link to the workflow run, and, if the report is too long for a
  comment (GitHub's limit is 65,536 characters), a note that it was cut. In the comment, a
  zero-width space follows every `@`, so a trace cannot mention, and so notify, a user or a team.
* **What is not neutralised.** GitHub turns bare URLs and `www.` addresses in text into links
  (its "autolink" extension); a run name holding a URL can therefore show as a link in the summary
  and the comment. The text is the run name as recorded; no markup or HTML gets through.
* Like every report of the gate, the reports never contain content, and error messages are
  secret-scrubbed ([reliability-gate.md](reliability-gate.md)).

## Pinning and the Marketplace

Pin the action to a full commit SHA you have reviewed (`@<40-hex-sha>`), or a tag; `@master`
follows the branch. The action installs aobs-server from that same commit.

The action lives in a subdirectory of this repository, so it is used by path
(`AshmitJain932/ai-agent-observability/actions/gate@<ref>`). The GitHub Marketplace lists only
actions whose `action.yml` is at the root of their own public repository, one action per
repository, so a Marketplace listing needs a separate repository; until then the action is used
by path.

## Tests and limits

* `server/tests/test_gate_action.py` runs the action's scripts (`actions/gate/gate.sh`,
  `actions/gate/comment.py`) locally on the bundled example traces, with the runner's files
  (`GITHUB_OUTPUT`, `GITHUB_STEP_SUMMARY`, the event payload) simulated and `gh` replaced by a
  local fake: pass, fail, report-only, baseline, no verdict, invalid inputs, hostile file and run
  names, and the comment's create, update, fork, read-only-token and size cases.
* `.github/workflows/gate-action.yml` uses the action by path (`uses: ./actions/gate`) on the
  bundled traces when the action or the gate changes: one job expects PASS, one expects FAIL (the
  enforced step must fail), one requests a comment without `pull-requests: write`. At the time of
  writing it has not yet run on GitHub's runners.
* The scripts are bash and were tested on Linux; macOS and Windows runners have not been tried.
* The step installs aobs-server on every run (no cache); expect it to take a few seconds to a
  minute, depending on the package index.
