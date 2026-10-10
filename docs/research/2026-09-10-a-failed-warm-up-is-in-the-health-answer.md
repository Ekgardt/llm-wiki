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

## 2026-10-05: foreground work preempts full-path warming

The original measurements above remain historical. A real captured corpus with
126,298 chunks and a complete graph reproduced a cold ordinary MCP refusal at
14.033 seconds and a repeated BASE answer at 8.374 seconds. The transparent
trace showed reranker loading (9.981 seconds) and a full warm-up search competing
with foreground FTS work and owning the shared dense slot. A separate diagnostic
without startup warming returned BASE in 8.917/3.760 seconds. These ordered runs
shared CPU with pytest and filesystem caches; their difference is not an isolated
speed claim, and disabling warming is not the proposed product fix.

Relevant primary sources, checked 2026-10-05:

- [Python 3.10.22 threading](https://docs.python.org/3.10/library/threading.html):
  condition notifications and events support cooperating ownership; native
  threads cannot safely be forcibly interrupted.
- [PyTorch CPU threading](https://docs.pytorch.org/docs/2.14/notes/cpu_threading_torchscript_inference.html):
  concurrent inference competes for CPU. This is current documentation, not a
  claim that the installed runtime uses that documentation's version.
- [Microsoft bulkhead pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead):
  retain per-kind capacity and actual ownership instead of enlarging pools.

The candidate keeps full-path, two-pass warming and actual completed stage-cost
observations. Process-local foreground leases cover a search and every optional
worker it starts, including workers that outlive a BASE fallback. Warming waits
for those owners to settle. A foreground search signals current warm-up tokens;
existing cancellation checks then stop cooperating SQL/reading work. Each retry
keeps the original warm-up stage clock. The existing 10 ms cancellation cadence
is shared with the condition wait to observe shutdown; ownership notifications
wake it immediately. No new setting, budget, runtime path, schema or daemon is
introduced.

Cancellation is checked before optional work starts and before its result can
become a successful cost observation. A short *waiting* deadline is different
from an expired original caller deadline: valid unknown-cost stragglers remain
allowed, including an already expired waiting share created by the tail reserve.
They retain ownership until actual settlement. A reranker accepts an optional
cooperative cancellation callback on its built-in path, checks it before/after
native batches, and preserves the fused order if cancellation invalidates partial
scores. Custom scorers receive their original arguments; their late cancelled
results are also discarded. An already running native forward or model load
cannot be forcibly interrupted, and its capacity is never released early.

Original race and cancellation failures, an initially incorrect WAIT/caller clock
interpretation, and subsequent regression results are retained in private audit
reports. This candidate qualification is not proof that the real cold MCP path
now fits fourteen seconds or that HYBRID fits its optional share. The merged
product still requires real first/repeated calls, measured costs and unchanged
strict artifact validation before installation or closure.
