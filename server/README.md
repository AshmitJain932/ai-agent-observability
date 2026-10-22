# aobs-server

The server side of aobs, observability for AI agents. Early-stage software (alpha).

- `aobs-server analyze`: explain a recorded run (errors, repeated failures, wasted calls,
  bottlenecks) with evidence; content is never shown.
- `aobs-server compare` and `compare-groups`: find regressions between runs, with statistics.
- `aobs-server gate`: check runs against requirements in a TOML policy; exit code 3 on FAIL, with
  JUnit XML and Markdown reports for CI.
- `aobs-server dashboard`: a read-only web dashboard over trace files (`--data-dir`) or stored
  traces. Sample datasets are installed with the package.
- `aobs-server serve`: the collector, an authenticated ingestion API that stores runs in
  PostgreSQL 16 (not production-hardened: no rate limiting, retention job or built-in TLS).
- `aobs-server demo seed`: fill a local demo collector with the sample traces. The repository's
  `compose.yaml` runs the whole demo with `docker compose up` (docs/docker.md).

Record runs with the `aobs` SDK. Documentation:
<https://github.com/AshmitJain932/ai-agent-observability>

Licensed under the Apache License 2.0.
