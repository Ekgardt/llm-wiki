# Codex turn capture policy

Owns the existing `scripts/codex_turn_end.py` in place and its focused policy tests.
The module uses only the standard library. Callers supply a mutable state mapping,
session information and the current datetime; it performs no IO or clock read.

`claim_turn_end` claims the first eligible capture; another turn inside the existing
30minute window records a pending tail. `claim_quiet_tail` selects the first lexical
eligible session other than the caller and returns its prior entry for rollback.
`mark_captured` replaces that session's bookkeeping. `snapshot`/`restore` preserve
entry references; they do not promise deep copies. `restore(..., None)` removes the
claim. Invalid timestamp arithmetic retains the existing infinitely-old fallback.

Only expired, valid capture-only entries retire. Pending tails, unknown fields,
invalid timestamps and future captures remain protected. The old September17
research describes a superseded64-entry trimming policy; it is retained as history.
The current aggregate state writer may refuse an oversized mapping. This policy
module does not discard pending evidence to make the state fit.

`codex_memory.py` owns state locking, actual durable capture and rollback after a
capture failure. A returned claim is not proof of publication. The original tests
in `test_a_pending_codex_tail_is_not_a_cache_entry.py` and
`test_a_codex_turn_is_not_a_session.py` still exercise those real boundaries;
each is owned by its own test module.

Complete packet: `codex-turn-policy-context.json`; no implementation-source
exception. Its tests include four unchanged original regressions, rollback/time
boundary cases, checked static imports and isolated subprocess import. Production
source and original function AST equality are verified separately in retained logs.

Run `python -m pytest tests/modules/codex_turn_policy
 tests/test_a_pending_codex_tail_is_not_a_cache_entry.py
 tests/test_a_codex_turn_is_not_a_session.py` (join as one shell command).
No new API, facade, dependency, state path or installation.
See [research](../research/2026-11-03-codex-turn-policy-owner.md).

## Structural reference retained from the overview

[Codex turn policy](codex-turn-policy.md) owns the existing in-memory
capture-window and pending-tail decisions in `scripts/codex_turn_end.py`.
It receives state and time; `codex_memory.py` retains locking, durable state,
ingress and failed-capture restoration. The four original pure regressions move
to its packet; disk/ingress integration assertions remain intact. Historical
September17 count-based trimming is superseded by protection of pending/unknown
entries, not reinstated. No cadence, runtime path or hook registration changes.
