# A live-corpus budget refusal is not index corruption

Date: 2026-10-01. Scope: diagnostic classification; no budget or settings contract changes.

A valid immutable generation followed by a live-corpus scan past its file-count
or total-byte budget returned `error: catalog or active artifacts are invalid`.
The artifacts had already passed validation. Two old-code regressions reproduce
this on real generated SQLite artifacts (2 failed). A deferred source read, used
by pathname traversal, separately reproduces the same untyped byte refusal.

Use `CorpusCapacityExceeded`, a subclass of the existing ValueError contract,
at all three enforcement points for the two existing configurable budgets. Keep
the existing refusal messages and thresholds. Carry the setting, its limit and
the minimum already observed. Doctor handles this type only after validating the
selected generation: report degraded, incomplete freshness, the exact exhausted
budget and a request to review that budget. Do not claim freshness, successful
collection or a complete index refresh. Do not rebuild validated artifacts solely
because this diagnostic encountered a capacity refusal. Malformed manifests,
damaged artifacts, unexpected ValueErrors and security refusals retain their
existing handling. No source, generation, quota, timeout, schema, runtime path,
environment name or MCP tool is removed, raised or added.

Sources checked 2026-10-01:

- [Python 3.10 exceptions](https://docs.python.org/3.10/library/exceptions.html)
  describes exception subclasses and preservation of parent exception handlers.
- [Microsoft health endpoint monitoring](https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring)
  distinguishes checked components and meaningful diagnostic information.
- [Google SRE monitoring](https://sre.google/sre-book/monitoring-distributed-systems/)
  separates observed symptoms from causes and discusses saturation signals.

These sources support explicit failure categories and honest incomplete checks;
they establish no numeric optimum for the existing budgets.

Alternatives: increasing limits hides the diagnostic defect and needs a numerical
basis; rebuilding an intact index repeats work without fixing a quota; accepting
all ValueErrors as degraded would conceal actual corruption; matching error text
would couple behavior to incidental message spelling. A compatible exception
subclass distinguishes the exact resource refusal without relaxing validation.

Candidate qualification: 173 related corpus/generation/whole-source complexity
and branch tests passed, 3 skipped. New tests cover both budgets, public code-source
collection and a real sealed deferred source read. Existing corruption controls
remain unchanged. Changed/new functions measured by actual Lizard have CCN 1-4;
Ruff passed after correcting import ordering. Initial measurement-harness function
counts were incorrect and their failures are retained; the successful measurement
and the separate complete branch/complexity guards are the qualification.

This does not establish the cause of the installed Doctor's transient 16:46
catalog-invalid observation. An exploratory source-count probe used an explicit
2000-file bound, not the configured 10000; the corrected configured-bound probe
validated the selected generation and reported it stale. Preserve those distinct
observations. The later installed generation rebuild exhausted its existing
900-second budget with `generation retrieval cancelled`; it did not report
successful repair. The complete local regression snapshot f7b34186 predates this
candidate and had one deadline-test failure in its first parallel pass; its
unchanged failed-shard repeat is tracked separately. No complete current-source
regression pass is claimed by the candidate qualification above.
