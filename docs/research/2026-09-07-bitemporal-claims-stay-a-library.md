# Should `bitemporal_claims` be wired into the answer? — 2026-09-07

## The question

`scripts/bitemporal_claims.py` reads a claim ledger on two clocks and derives
where belief in a claim ends; today it also weighs reliability before a later
claim closes an earlier one. Nothing imports it. The owner asked whether to
wire it into the answer path and left the decision to the rules.

## What wiring would change, measured

- The installed vault has 32 pages with claim ledgers, 61 claims, all
  `active`. `history()` over all of them closes **0 of 61** — no two claims
  share a `(subject, relation, qualifiers)` key with different values. Read
  through the bitemporal lens, the vault says exactly what it says without it.
- The grounded answer path (`query_memory.grounded_qa`) reads page spans, not
  claims, and cites bytes. A claim's derived end would have to become a
  filter on spans, which is a new contract between two subsystems.
- The LongMemEval stand ingests sessions as daily entries with no claim
  ledgers at all; the measurement that decides changes could not see this
  one.
- Write-time closing already exists where the product needs it:
  `contradiction_pipeline._supersede_ledger_claims` sets
  `lifecycle: superseded` and `_mark_page_superseded` sets `status:
  superseded` / `superseded_by`; `search_memory` excludes retired pages.

## Decision

Not wired. A change that no measurement can see and no live claim would
exercise fails rule 4 on its face — cost without a measurable return — and
rule 2's research found the field's answer for knowledge updates was
assembly, not storage (arXiv:2606.01435). The module stays: it is the
approved reading of 2026-08-28, it is tested, and its `reliability` order is
the one the contradiction pipeline should adopt when it next decides which
of two open claims to close. That adoption is the place to wire it, and it
will be measurable there (claims closed, claims held open).

Revisit when a live vault shows two open claims on one key — the number to
watch is "expired by a successor" in the count above.
