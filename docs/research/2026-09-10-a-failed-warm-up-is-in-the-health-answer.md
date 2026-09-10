# A failed warm-up is in the health answer

Date: 2026-09-10. Trigger: audit finding OPS-13. `mcp_server.warmup_retrieval_path`
wrapped the reranker load and both warm passes in
`contextlib.suppress(BaseException)`, and the warm thread in
`suppress(Exception)`; nothing was logged or recorded. A failed warm-up
means the first questions of a session answer from the lexical leg alone —
the very symptom measured on 2026-08-24/26 — and neither the user nor the
doctor nor the `llm-wiki://health` resource could see why.
`mcp_http._shutdown` suppressed `BaseException` the same way.

## Sources

1. This repository's own rule for capture (`knowledge/notes/observable-capture-and-bounded-maintenance-decision.md`):
   a failure that must not break the caller is recorded, not swallowed.
2. Python documentation, `contextlib.suppress`: it "suppresses any of the
   specified exceptions" — with `BaseException` that includes
   `KeyboardInterrupt`, `SystemExit` and `MemoryError`, which is never what a
   warm-up wants. https://docs.python.org/3/library/contextlib.html#contextlib.suppress
3. The health resource already carries the compile status and degrades its
   quality with a warning when the compile is unknown or behind
   (`_compile_health_quality`); a warm-up that failed belongs in the same
   answer.

## Decision

1. `warmup_retrieval_path` records its state in one module-level record —
   `not_started`, `running`, `warm` (with seconds), or `failed` (stage and a
   redacted error) — catching `Exception` only, and prints one line to
   stderr on failure.
2. `_vault_status` carries `warmup`; the health resource's quality gets a
   warning and `partial` when the warm-up failed.
3. `mcp_http._shutdown` catches `Exception` and reports it to stderr.

Files: `scripts/mcp_server.py`, `scripts/mcp_http.py`, `tests/test_mcp_server.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-operations-and-reliability.md`.
