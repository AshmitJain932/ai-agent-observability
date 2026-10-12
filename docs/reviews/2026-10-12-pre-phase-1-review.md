# Pre-Phase-1 design review (2026-10-12)

Scope: the four Phase 0 documents (architecture, event model, roadmap, decisions), checked for
contradictions, missing requirements, security problems, data-integrity risks and roadmap gaps,
and against the five proposed differentiators. Every finding below was resolved in the documents
in the same commit as this file; "Where" names the section that now carries the fix.

The architecture itself stands: native event schema with OTel-compatible IDs, one FastAPI service,
PostgreSQL, stdlib-only SDK, metadata-only default. No finding required replacing it.

## A. Contradictions

| # | Finding | Resolution | Where |
|---|---|---|---|
| A1 | "Conflicting duplicate: first end wins" contradicts the requirement that the stored result is identical for any arrival order (and the Phase 1 permutation test). Arrival order is not meaningful under retries, so "first" is not well defined. | Conflicts resolve by a **content-derived total order** (lowest SHA-256 of the canonical JSON wins) and are counted. Same result for every order. | event-model §9, architecture §5/§10, ADR-009 |
| A2 | `truncated` appeared both as a content `status` value and as a boolean. | `truncated` is only a boolean flag; it can combine with `captured` or `redacted`. | event-model §7 |
| A3 | Usage example used `null` values while rules said "absent or null". Two spellings of "unknown" invite bugs. | Unknown token counts are **absent**. `null` is rejected. | event-model §5, §6 |
| A4 | Trace status was defined in terms of "the root", but several roots per trace are allowed. | Status defined over all roots (forest), plus explicit `no_root` handling. | event-model §9 |
| A5 | "Orphans shown under the root" is ambiguous with zero or several roots. | Orphans are listed in a separate "missing parent" group; never attached to any root. | architecture §10 |
| A6 | `parent_span_id` was required "if not root", which the server cannot check: an omitted parent looks exactly like a root. | The key is **always required**; `null` explicitly means root. | event-model §4 |
| A7 | "Unknown fields are preserved, never rejected" for every minor version means typos like `stauts` are silently accepted. Also "minors may add enum values" breaks older servers that validate closed enums. | Unknown fields are **rejected** when the payload's minor ≤ the server's minor and **preserved** only when the payload is newer. Fields minors may extend are typed as open vocabularies (pattern-checked strings); closed enums change only in a major. | event-model §10, ADR-010 |
| A8 | Phase 1 listed "SDK dataclasses", but the SDK package is created in Phase 2. | SDK serializer joins the same conformance suite in Phase 2. | roadmap Phase 1/2 |
| A9 | The trace assembler is needed by both ingest (merge) and query (tree), but the dependency rule forbids ingest↔query imports. | New pure package `aobs_server.model` (wire models, versioning, merge, assembly) with no I/O; both ingest and query depend on it. | architecture §3 |

## B. Security and privacy

| # | Finding | Resolution | Where |
|---|---|---|---|
| B1 | Plain SHA-256 "fingerprints" of tool arguments are reversible for low-entropy inputs (`{"order_id": "1042"}` can be brute-forced), so metadata-only mode would leak content. | Fingerprints are **HMAC-SHA256** with a key held by the SDK (random per process by default, or a configured stable key). The key id is part of the value so only comparable fingerprints are compared. | event-model §5, architecture §9, ADR-011 |
| B2 | Exception messages were captured in metadata-only mode without saying so; they often contain personal data ("user jane@… not found"). | Documented explicitly; new `resource.capture.error_messages` flag (default on, scrubbed); server ceiling applies to it too. | architecture §9, event-model §3 |
| B3 | Any ingest key could write any `environment`, so a developer laptop could pollute `production` data. | Keys may be **bound to an environment**; a mismatched batch is rejected. | architecture §5 |
| B4 | Python's `json` module accepts `NaN`/`Infinity` by default; these are not JSON and break numeric aggregates. | Strict parser rejects non-finite numbers. Implemented in Phase 1. | event-model §10 |
| B5 | `end_user_id` would store personal data before any governance exists to erase it. | Removed from v1. Revisit with Section 8 (erasure by user). Guidance: do not put personal identifiers in attributes. | event-model §5 |

## C. Data integrity

| # | Finding | Resolution | Where |
|---|---|---|---|
| C1 | Retention keyed on `started_at` (client clock) can be evaded or triggered early by a wrong clock. | Retention keys on server `received_at`. | architecture §6 |
| C2 | Late events after a trace deletion would silently re-create the trace (and defeat deletion requests). | **Tombstones**: deleted `(project_id, trace_id)` pairs are recorded; later events are dropped and counted. | architecture §5/§6 |
| C3 | Refreshing the trace summary in the ingest transaction races under READ COMMITTED: two concurrent batches for one trace each recompute from a snapshot missing the other's spans, and the last writer wins with a wrong summary. | Take a per-trace row lock (`SELECT … FOR UPDATE` on the summary row, inserted first) before recomputing. | architecture §5 |
| C4 | A looping agent can produce an unbounded trace. | Per-trace span cap (default 10,000); excess dropped, trace flagged `truncated`. Loops stay visible as "hit the cap". | architecture §5 |
| C5 | Parent pointers can form cycles (including a span naming itself as parent) — not expressible in JSON Schema, and a naive tree walk would never terminate. | Assembler detects cycles; affected spans are reported as orphans with `cycle` reason. Tested. | event-model §9 |

## D. Roadmap gaps and dependencies

| # | Finding | Resolution |
|---|---|---|
| D1 | The five differentiators were absent from the plan. | Added Phases 9–11 after Section 1's core (roadmap). |
| D2 | **Dependency:** an adaptive-telemetry prototype cannot show it reduces overhead without a way to measure overhead first. | The internal overhead *measurement harness* is built in Phase 2 (SDK micro-benchmarks) and Phase 8 (end-to-end harness) and is a prerequisite of Phase 9. The user-facing overhead *reporting* feature stays in Phase 11. |
| D3 | Dashboard authentication in Phase 4 was undefined. | Default (P2 unanswered): self-hosted; dashboard uses read-scoped keys; real user login in Phase 8 or when SaaS is chosen. |
| D4 | Emitting `span_start` for every span roughly doubles event volume for short spans. | SDK **start coalescing** (Phase 2): if a span ends before its start event was exported, only the end is sent. Long-running spans still announce themselves. |

## E. Fields removed or simplified (smaller v1)

| Before | After | Reason |
|---|---|---|
| `end_user_id` | removed | B5 |
| `prompt {name, version}` | reserved attributes `aobs.prompt.name` / `aobs.prompt.version` | Not used by any Section 1 feature; attributes carry it without schema surface |
| `tool.mcp {server, method}` | attributes `mcp.*` | Same; OTel has MCP attribute conventions to align with later |
| `llm.request.tool_count` | removed | No Section 1 use |
| `tool.arguments_fingerprint` | span-level `input_fingerprint` (HMAC) | One generic field serves tool arguments, LLM requests and retrieval queries — needed for waste detection without a schema change per kind |

Added: `usage.total_tokens` (some providers report only a total — without it the value is lost);
`resource.capture.error_messages` (B2); `sent_at` is now required (clock-offset estimation).

## F. Readiness for the differentiators

See architecture §13. Summary: nothing in v1 blocks them. What they need that v1 already has:
monotonic durations, start/end lifecycle, separate content path, HMAC input fingerprints,
"absent ≠ zero" semantics. What they will add later as **minor** (backward-compatible) schema
versions, with defined defaults for old data: sampling/capture-decision metadata and SDK
self-measurement events. Nothing is added now "just in case".
