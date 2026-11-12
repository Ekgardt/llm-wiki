# Codex turn capture policy: qualify the existing in-memory boundary

Date: 2026-11-03. Internal boundary under the approved decomposition plan.

## Responsibility and evidence

`scripts/codex_turn_end.py` already depends only on the standard library and
mutates the caller-supplied mapping using a caller-supplied timestamp. It neither
reads the clock nor locks, writes or publishes a capture. `codex_memory.py` owns
the state transaction, fallback when state admission fails, capture ingress and
restoration on capture failure. Keep that direction and both existing paths.

Qualify the existing source in place as codex-turn-policy. Move exactly four
pure policy regressions and their shared pending-entry fixture from
`tests/test_a_pending_codex_tail_is_not_a_cache_entry.py` into the packet; retain
all three disk/ingress regressions in that file and run them with the unchanged
`test_a_codex_turn_is_not_a_session.py`. Preserve every original function AST and
production byte. Integration reuses the same fixture through an explicit import.
No integration assertion becomes a mock, disappears, or counts as independently
qualified: those integration files remain whole-project ownership obligations.

The before suite passes12tests. Baseline ASTs and source hash are retained in
logs/codex-turn-policy-before-ast.json. Builtin graph refresh is required before
implementation; supplement its partial callers with source inspection of
_claim_capture, _turn_end_plan, _restore_claim and their tests.

## Contract and qualification

Keep the existing30minute cadence, lexical quiet-tail selection, caller exclusion,
invalid-timestamp treatment and snapshot/restore reference semantics. Expired
valid capture-only entries may retire; unknown entries and pending tails remain
protected. A claim is bookkeeping, never evidence of durable publication. No
second state owner, timer, queue, environment change or live installation.

The original September17 research remains an immutable historical description;
its64-entry policy was superseded by the retained-tail regressions. The new guide
must distinguish current behavior from that history, not silently rewrite it.

Add a checked-source import boundary and actual complete packet measurement.
Exercise rollback and exact time boundaries with the existing implementation.
Measure new/moved test complexity, keep original AST evidence and run real
state/ingress integrations. All required instructions and full local dependency
source remain in the packet; no qualified-contract exception is used.

## Alternatives and sources

Moving production code creates an unnecessary compatibility facade. Pulling the
state writer into this module couples policy to publication and its large source
closure. Merely excluding the four original tests loses required context. The
selected boundary follows the responsibility already present in production.

Three independent primary publishers checked2026-11-03:
- Python datetime: https://docs.python.org/3.14/library/datetime.html — aware versus
  naive timestamps and subtraction behavior are material to existing error handling;
  retain these semantics, without a new timezone or clock abstraction.
- pytest good practices: https://docs.pytest.org/en/stable/explanation/goodpractices.html
  — test import and discovery paths matter; validate both relocated unit tests and
  unchanged integration collection in the qualified Python3.14 environment.
- Fowler bounded context (2014): https://martinfowler.com/bliki/BoundedContext.html
  — make model boundaries explicit; this supports separating in-memory policy from
  persistent state ownership, not a claim that one pattern proves safety.

No new dependency, version bump, release or change to host hook registration.

Builtin refresh completed before implementation: generation
`generation-18e48d6f5373fe69-3e19930d`, source commit6fb0ca27. Static callers:
claim_turn_end2, claim_quiet_tail2, mark_captured7; generic snapshot/restore
queries also include unrelated same-name symbols, so direct source verification
of codex_memory.py remains necessary. Evidence: logs/codex-turn-policy-graph.json.
