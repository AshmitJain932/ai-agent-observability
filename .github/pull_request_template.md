## What and why

<!-- What does this change, and why is it needed? Link the issue if there is one. -->

## How it was tested

<!-- Commands you ran and what they showed. Say precisely what was tested against what
     (e.g. "real openai 3.29.0 SDK against a mocked HTTP transport"). -->

## Checklist

- [ ] `uv sync --locked` and `uv lock --check` pass (dependencies added with `uv add`, not by hand)
- [ ] `uv run ruff check .` and `uv run ruff format --check sdk server examples schema` pass
- [ ] `uv run mypy --strict sdk/python/src server/src` passes
- [ ] `uv run python -m aobs_server.model.schema_lock` passes (schema changes: `schema/CHANGELOG.md` updated)
- [ ] `uv run pytest -q -o faulthandler_timeout=300` passes, with new tests for the change
- [ ] Docs updated for user-visible changes (`uv run --group docs mkdocs build --strict` if `docs/` changed)
- [ ] No real side effects, network calls to providers, API keys or secrets in tests or examples
- [ ] No safety invariant or test weakened, skipped or removed to make this pass
