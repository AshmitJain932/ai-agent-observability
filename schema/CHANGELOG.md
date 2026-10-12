# Wire schema changelog

Every change to `v1/ingest.schema.json` needs an entry here and an updated `v1/SCHEMA_LOCK`
(the test `test_schema_lock.py` fails otherwise). Compatibility rules: docs/event-model.md §10.

To update the lock after an intentional change:

```
uv run python -m aobs_server.model.schema_lock --write
```

### Clarifications to 1.0 (no wire change, no lock change)

- 2026-10-12 (Phase 2): `usage.input_tokens` includes cached tokens; `cache_read_input_tokens` /
  `cache_write_input_tokens` are subsets of it, `reasoning_tokens` a subset of `output_tokens`
  (docs/event-model.md §5).

## 1.0 — 2026-10-12

Initial version. Event types `span_start`, `span_end`, `span_event`; envelope with `resource`
and `capture` declaration; span blocks `agent`, `llm`, `tool`, `retry`, `error`, `usage`,
`cost`, `content`; HMAC `input_fingerprint`; bounded `attributes`.
