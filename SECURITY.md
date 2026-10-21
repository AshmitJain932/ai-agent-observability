# Security policy

## Supported versions

This project is pre-release (`0.1.0.dev0`) and maintained on a best-effort basis. Security fixes
go to the `master` branch only; there are no maintained release branches yet.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Report it privately through GitHub: on this repository's **Security** tab, choose **Report a
vulnerability** (GitHub private vulnerability reporting). Include:

- what is affected (the SDK `aobs`, `aobs.safety`, the collector `aobs-server`, the dashboard or
  the CLI) and the version or commit;
- how to reproduce it, ideally with a minimal script or request;
- what an attacker could do with it.

You will get an acknowledgement when the report has been read. There is no guaranteed response
time. Please give a reasonable time for a fix before disclosing the problem publicly.

## Scope

In scope, for example:

- secrets, prompts or other content reaching traces, logs, the database or the dashboard when the
  privacy settings say they should not (redaction bypasses);
- reading or writing another project's data in the collector (tenant isolation, API-key scopes);
- ways for `aobs.safety` to execute a denied, unapproved or duplicate action, or to report an
  outcome that did not happen;
- recovering identifiers from an `aobs.safety` SQLite store without its secret.

Not vulnerabilities on their own:

- the fake keys, passwords and card numbers in the tests and examples (placeholders for redaction
  tests; none is a real credential);
- limits the documentation already states: for example, `aobs.safety` is not exactly-once and
  relies on the provider honouring idempotency keys, the SQLite store coordinates one host only,
  and the collector has no rate limiting or built-in TLS yet.

## Design references

The privacy and security model is described in
[docs/architecture.md](docs/architecture.md) (section 9), [docs/ingestion.md](docs/ingestion.md)
and [docs/traces-api.md](docs/traces-api.md). The action-safety guarantees are in the module
documentation of `sdk/python/src/aobs/safety.py`.
