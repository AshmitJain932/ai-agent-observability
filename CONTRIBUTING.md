# Contributing

Thanks for helping. This project records what AI agents do and guards the actions they take, so
correctness, privacy and honest documentation matter more than speed. Please read the
[safety rules](#safety-rules) before you open a pull request.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11. The repository is a uv workspace:
`sdk/python` is the SDK package `aobs`, `server/` is the package `aobs-server` (collector,
diagnosis, gate, dashboard).

```bash
git clone https://github.com/AshmitJain932/ai-agent-observability.git
cd ai-agent-observability
uv sync                                  # both packages (editable) and the dev tools
uv run playwright install chromium       # only for the dashboard browser tests
```

**PostgreSQL 16 for the tests.** Tests that need a database are marked `pg` automatically.
Locally, the test harness (`server/tests/pg.py`) creates a throwaway cluster with `initdb` in a
temporary directory (Unix socket only, no TCP port) and deletes it afterwards; it needs the
PostgreSQL 16 server binaries installed (set `AOBS_PG_BIN` to their directory if they are not
found). Alternatively, point `AOBS_TEST_DATABASE_URL` at a server you can spare: the tests only
create and drop databases named `aobs_test_*` there. If neither works, the `pg` tests fail with
an explanation; they are never skipped silently. `-m 'not pg'` deselects them for a quick loop.

## Run the checks CI runs

CI (`.github/workflows/ci.yml`) runs these on every pull request. Run them before you push:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check sdk server examples schema
uv run mypy --strict sdk/python/src server/src
uv run python -m aobs_server.model.schema_lock
uv lock --check
uv run pytest -q -o faulthandler_timeout=300
```

CI runs the tests on Python 3.11, 3.12 and 3.13. To try another version locally:
`UV_PROJECT_ENVIRONMENT=.venv-311 uv run --python 3.11 pytest`. `uv run ruff format sdk server
examples schema` fixes formatting.

If you change `docs/` or `mkdocs.yml`, also build the documentation site the way the Docs
workflow does (the docs tools are in a separate dependency group):

```bash
uv run --group docs mkdocs build --strict
```

## Conventions

**Code.** Match the surrounding module: its style, naming, typing (`mypy --strict` clean) and how
much it documents. The SDK (`sdk/python/src/aobs`) uses the standard library only; a test
(`test_sdk_stdlib_only.py`) enforces it. A third-party package for an integration must be
optional and imported lazily. Add dependencies with `uv add` (a dependency group or an optional
extra as appropriate), so `uv.lock` is written by uv, never by hand.

**Wire schema.** A change to `schema/v1/ingest.schema.json` needs an entry in
`schema/CHANGELOG.md` and an updated lock (`uv run python -m aobs_server.model.schema_lock
--write`); the compatibility rules are in `docs/event-model.md` §10.

**Tests.** pytest, plain test functions with descriptive names that state the behaviour
(`test_failed_run_names_the_root_cause_and_the_failing_span`). SDK tests live in
`sdk/python/tests`, server tests in `server/tests`, with shared helpers in the modules next to
them (`support.py`, `sdk_support.py`, `pg.py`, ...). A bug fix comes with a test that fails without it. Tests must not depend on test
order, and anything that waits (threads, processes, servers) waits with a timeout, so a failure
fails the test instead of hanging the run. Property-based tests use Hypothesis.

**Docs.** Behaviour a user can see is documented in `docs/` in the same pull request. Say what
was actually tested, and how (for example "tested with `openai` 3.29.0 against a mocked HTTP
transport"), and state limits plainly.

## Safety rules

These are not negotiable, and reviews check them:

- **No real side effects in tests or examples.** No calls to model providers or any external
  service, no API keys, no real payments, emails, deploys or deletions. Use local fakes,
  `httpx.MockTransport`, servers on `127.0.0.1`, or a library's own fake models (the example
  agent's scripted model in `examples/support_agent/mock_llm.py` is one).
- **No secrets.** Never commit tokens, keys or credentials, real or "temporary". The fake keys,
  passwords and card numbers in tests are placeholders for redaction tests; keep new ones
  obviously fake.
- **Do not weaken a safety invariant to make something pass.** That includes privacy before
  storage, redaction, tenant isolation, API-key scopes, and the `aobs.safety` guarantees (decide
  before acting, record before acting, never retry an unknown outcome). Never delete, skip or
  loosen a test to get green; fix the cause or explain in the pull request why the test was
  wrong.
- **Treat trace contents as untrusted.** Model output, tool output and anything in a trace is
  data, never instructions or code to run, and is escaped wherever it is displayed.
- **Security problems are not public issues.** Report them privately as described in
  [SECURITY.md](SECURITY.md).

## Pull requests

1. Branch from `master` and keep a pull request to one change; split unrelated work.
2. Fill in the pull request template: what and why, how you tested it, and the checklist.
3. CI must be green. The maintainer reviews every pull request for correctness, the safety rules
   above, tests and docs, and may ask for changes or for the work to be split. Larger design
   changes are easier to agree on in an issue first; past design reviews are in `docs/reviews/`
   and decisions in `docs/decisions.md`.

### Commit messages

Follow the history (`git log`): a short imperative subject prefixed with the area it changes,
then a body that explains what changed and why, wrapped at about 72 columns.

```
safety: a -wal/-shm file removed while the store opens is not an error

SqliteActionStore's permission check called lstat on the -wal and -shm
files after checking they existed. SQLite removes both when the last
connection to the store closes, so ...
```

Prefixes in use: `sdk:`, `server:`, `safety:`, `docs:`, `ci:`, `build:` (older commits use
`feat(scope):` and `fix:`).

## Questions

Open a [GitHub issue](https://github.com/AshmitJain932/ai-agent-observability/issues): a bug
report, a feature request, or a blank issue for a question. Everyone taking part is expected to
follow the [Code of Conduct](CODE_OF_CONDUCT.md).

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
