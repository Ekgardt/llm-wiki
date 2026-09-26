# The guide names the state recovery sets

Date: 2026-09-26. Audit 2026-09-26, state finding L5 (the guide says quarantine, the
code says conflicted).

## Fact

`docs/USER-GUIDE.md` said recovery "quarantines a target that matches neither its
recorded before nor after hash". The code marks such a transaction `conflicted`:
`MarkdownCoordinator._conflicted_from_mismatch` → `_conflicted`, with the code
`unknown_target_bytes` from `_before_mismatch_code` (`scripts/markdown_transaction.py`).
`quarantined` is a different state (a failed precondition or DLP refusal, rolled back).
Doctor and the `run/` deletion contract treat both as blocking, but an operator reading
the guide looks for the wrong one. The other half of L5 — the 90-day history prune was
undocumented — is covered by the guide paragraph added with
`docs/research/2026-09-26-a-new-operation-family-is-bounded-by-default.md`.

## Source

Diátaxis, "Reference", fetched 2026-09-26 from https://diataxis.fr/reference/:
"Reference material describes the machinery. It should be austere." and "There should
be no doubt or ambiguity in reference; it should be wholly authoritative."

## Decision

The sentence names the state and the code the machinery writes.

## Files

- `docs/USER-GUIDE.md`
