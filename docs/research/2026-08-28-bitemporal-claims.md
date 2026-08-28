# Bitemporal claims: what a validity interval has to mean before it can be trusted

Date: 2026-08-28. Roadmap item: `MEM-11`
(`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` section 12).
Written before the code, per rule 2.

## Why now — the measurement, not the fashion

`MEM-10` (commit `2704125`, note
`docs/research/2026-08-28-longmemeval-first-number.md`) is the first honest
LongMemEval number this vault has. Two of its category rows are the reason this
item exists at all:

| category | n | deterministic accuracy |
|---|---|---|
| temporal-reasoning | 13 | **0.154** |
| knowledge-update | 7 | **0.429** |
| overall | 50 | 0.320 |

26 of the 50 questions ended in `insufficient_evidence`. The MEM-10 note names
`temporal-reasoning` as one of the two categories "`MEM-11` (bitemporal claims)
is aimed at". So this is not a feature bet; it is aimed at a measured hole.

The competitor number to beat is Zep/Graphiti's: bitemporal edges carrying
`valid_at` / `expired_at` / `invalid_at`, with a claimed +15 points over Mem0
on temporal questions (63.8% vs 49.0% on GPT-4o) — recorded in
`docs/research/2026-08-27-number-one-memory-market-research.md`, which also
carries the standing caveat that these are *claims, not facts*: the 2026-08-23
audit reproduced published memory-system records at 92.3%→38.4% and
84%→58.4%.

## What the two clocks are

The distinction is old and standardised, and it is worth using the standard's
words rather than inventing new ones.

SQL:2011 splits time into two independent axes
([PostgreSQL wiki, SQL2011Temporal](https://wiki.postgresql.org/wiki/SQL2011Temporal),
[Microsoft SQL Server temporal tables](https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal/overview)):

- **Application time** (valid time) — "tracks the history of the thing out in
  the world". Declared `PERIOD FOR valid_at (valid_from, valid_til)`. It is
  written by the application, because only the application knows when a fact
  was true.
- **System time** (transaction time) — "tracks the history of when the database
  itself was changed". Declared `WITH SYSTEM VERSIONING`. It is written by the
  system and **never by the application**, because it is a record of the
  system's own knowledge.
- Used together, the two give a **bitemporal** table.

Three details from the standard matter to this design:

1. **Periods are half-open.** "Conventionally, temporal tables use
   closed/open intervals: the start time is included, but the end time is
   excluded… `[start,end)`." The reason given is exactly the one that matters
   here: adjacent records for the same entity "snap together without any
   special case logic". A claim ending where its successor begins must leave no
   gap and no overlap.
2. **A period implicitly adds a CHECK constraint forcing the start column to be
   strictly before the end column.** Empty and inverted intervals are not data;
   they are errors.
3. The temporal predicates are `CONTAINS`, `OVERLAPS`, `EQUALS`, `PRECEDES`,
   `SUCCEEDS`, `IMMEDIATELY PRECEDES`, `IMMEDIATELY SUCCEEDS`, and `CONTAINS`
   accepts a scalar datetime as its second operand. "What was true as of DATE"
   is `valid_period CONTAINS DATE` — a primitive, not an invention.

Graphiti/Zep carries the same two axes on graph edges
([Graphiti bi-temporal model](https://mintlify.wiki/getzep/graphiti/concepts/temporal-model)):
`valid_at` ("when the fact became true") and `invalid_at` ("when the fact
stopped being true") on the world axis; `created_at` ("when the edge was first
created") and `expired_at` ("when the edge was superseded/invalidated") on the
system axis. On contradiction it does **not delete**: it sets `invalid_at` on
the old edge to the new fact's `valid_at`, sets `expired_at` to the current
time, and creates the new edge.

That invalidation *rule* is right and this design copies it. What this design
does not copy is who applies it. The same page says the contradiction is
decided by an LLM: "An LLM analyzes the contradiction during edge resolution."

## Why the conflict decision here is deterministic

This vault has a recorded line against exactly that, and the market-research
note states it plainly under **Детерминированная свежесть** ("Don't Ask the LLM
to Track Freshness"): conflict resolution without an LLM "ровно наша линия" —
precisely our line. `MEM-14` exists to measure it. Putting an LLM in the
invalidation path would contradict a recorded position, make the derived
index non-reproducible, and cost tokens on every rebuild of a *disposable*
cache — against rule 4.

So the conflict decision is a key comparison, and nothing else:

> Two active claims conflict when they share a **bitemporal key** — the
> normalised `(subject, relation, qualifiers)` triple — and their canonical
> `value` bytes differ.

Every part of that is already canonical in `scripts/claims.py`:
`_semantic_payload` casefolds and NFC-normalises subject and relation, sorts
qualifiers by canonical JSON bytes, and normalises values through
`_normalize_value` (decimal strings, never binary floats). The comparison is
byte equality over `canonical_json_bytes`. Same input, same answer, forever.

## Where this design deliberately refuses

Copying Graphiti's rule wholesale would be wrong in one specific way, and this
is the part of the design worth arguing.

**Not every relation is single-valued.** `claim/v1` allows nine relations. A
later `member-of` claim does not contradict an earlier one — a thing belongs to
many groups; a module `uses` many modules; a package `depends-on` many
packages. Auto-invalidating those would silently delete true facts, which for a
memory system is the worse of the two failures (the vault's own `NEW-67` entry
in `knowledge/log.md` makes the same call: an unaccounted-for word must leave a
page *findable*, not invisible).

So only relations that are single-valued by construction take part in automatic
invalidation — `equals`, `has-state`, `has-value`, `located-at`, `starts-at`,
`ends-at`. The multi-valued three — `member-of`, `uses`, `depends-on` — carry
their own explicit `validity` and are never invalidated by a sibling. This is
strictly more conservative than the competitor and it is a deterministic table,
not a judgement.

The other refusals, all by name rather than by guess:

- **Ambiguous observation.** Two conflicting claims sharing a bitemporal key
  and the *same* `observed_at` cannot be ordered on the system axis. There is
  no correct winner, so there is no winner: refuse with
  `bitemporal_ambiguous_observation`. Picking one would fabricate an order the
  evidence does not carry.
- **Unparseable interval.** Anything `_canonical_time` rejects propagates as a
  refusal; nothing is coerced or defaulted.
- **Non-active lifecycle.** Only `active` claims are believed, matching
  `is_substantive` and `ClaimIndex._evidence_diagnostic`.

**Retroactive correction is not a refusal** — it is the whole point of having
two axes. A claim observed later but valid *earlier* is a correction of the
past. Its predecessor's effective valid end becomes the successor's valid
start, which for a full correction is at or before the predecessor's own start,
so the predecessor's effective interval becomes empty and it is believed at no
time at all. No special case is needed: `[start, min(end, invalid_at))` is
already empty when `invalid_at <= start`. And a query asked with an earlier
`known_at` still returns the pre-correction answer, because the correction was
not known yet.

## Why nothing is migrated

`claim/v1` is frozen. `scripts/schemas/claim-ledger-v1.json` sets
`"additionalProperties": false` on the claim record, `_EXTRACTION_FIELDS` is
compared by set equality, and `_require_canonical_semantics` recomputes the
`fingerprint` over the semantic payload — so a new field in the record would
change every fingerprint that carries it and invalidate every existing ledger.
The vault's rule is explicit: decisions are immutable, superseded, never edited
in place.

It does not need to change, because **both axes are already in the record**:

- valid time → `validity.from` / `validity.to`, already half-open and already
  order-checked by `_require_ordered_interval`;
- transaction time → `observed_at`, bound by `_require_observation` to the exact
  daily block the evidence was cited from, so it is not a field a writer can
  set freely — it is proved by the evidence.

Only the third thing is missing: `expired_at` / `invalid_at`, the *end* of the
system-time interval. And that must not be stored, because storing it would
mean **mutating an existing claim when a later one arrives** — the one thing
this vault forbids. It is a pure function of the ledger, so it is derived, the
same way `cache/claims.sqlite3` is already "a local owner-only SQLite
projection; Markdown ledgers remain canonical" and the same way
`knowledge/notes/derived-evidence-generation-decision.md` treats every index.

A claim carrying no explicit `validity` (`{"from": null, "to": null}`) is read
as valid from its own `observed_at` and never invalidated unless a conflicting
successor exists. That is the required backward-compatible reading, and it
needs no migration because it is a reading, not a rewrite.

## Measured, before writing anything

`_comparable_time` in `scripts/claims.py` compares canonical times **as
strings**, and canonical form keeps fractional seconds. Measured on this
machine:

```
_canonical_time('2026-08-19T00:00:00.500000Z') -> '2026-08-19T00:00:00.500000Z'
_validate_interval({'from':'2026-08-19T00:00:00Z','to':'2026-08-19T00:00:00.5Z'})
  -> REFUSED: claim validity must be a non-empty half-open interval
```

`'.'` (0x2E) sorts before `'Z'` (0x5A), so a sub-second interval compares as
inverted and a legitimate half-second validity is rejected. The direction is
safe — it refuses rather than accepts — but it is wrong, and a bitemporal
ordering built on the same comparison would be wrong in the *unsafe* direction:
it would order a successor before its predecessor. Ordering here parses to
`datetime` instead.

## Two things found while building this, both measured

Neither is fixed here. Both are why `MEM-11`'s own gate cannot close today.

**1. The vault holds no claim ledger at all.** `grep -rln '^## Claims' knowledge/`
returns nothing across the whole vault, and `cache/claims.sqlite3` does not
exist. The claim subsystem is written, tested and has never run on real data —
the same shape as `NEW-60` (generation vectors built only by tests) and
`NEW-71` (session records refused by a writer boundary).

**2. `ClaimPipeline` cannot read any daily log this vault writes.**
`claims._DATE_RE` is `^# (\d{4}-\d{2}-\d{2})`, so the first line of a claim
source must be exactly `# YYYY-MM-DD`. Measured on this machine, every daily
log the runtime produces starts with a title instead:

```
knowledge/daily/2026-08-20.md: # Daily Session Memory — 2026-08-20
knowledge/daily/2026-08-21.md: # Daily Session Memory — 2026-08-21
… 2026-08-24 … 2026-08-25 … 2026-08-26 … 2026-08-27 … 2026-08-28 (all the same)
```

`split_blocks` therefore raises `claim source has no canonical daily date` for
every one of them. This is finding (1)'s mechanism: no claim can be extracted
from this vault's own logs, so no ledger can exist. Deliberately not fixed
here — which of the two formats is canonical is a format contract, and this
vault's rule is not to improvise those mid-task. It needs its own decision:
either the daily writer emits the header the claim reader requires, or
`_DATE_RE` accepts the title form the whole vault already uses.

## What this does not claim

- It does not decide *whether* two differently-worded texts mean the same
  thing. The bitemporal key is structural. Entailment is not verified and is
  not claimed — the same boundary already recorded for the citation gate in
  `knowledge/notes/citation-relevance-gate-decision.md`.
- It does not make the LongMemEval temporal number go up on its own. The vault
  currently holds **zero** claim ledgers (`grep -rln '^## Claims' knowledge/`
  returns nothing, and `cache/claims.sqlite3` does not exist), so nothing
  bitemporal can reach an answer until claims are actually produced. Closing
  `MEM-11` by its own gate requires a paired measurement; this note and the
  code under it are the mechanism, not the measurement.

## Sources

- [SQL:2011 temporal features — PostgreSQL wiki](https://wiki.postgresql.org/wiki/SQL2011Temporal)
  — application-time vs system-time periods, `PERIOD FOR`,
  `WITH SYSTEM VERSIONING`, half-open `[start,end)` convention, the
  start-before-end CHECK constraint, and the `CONTAINS`/`OVERLAPS`/`PRECEDES`
  predicates.
- [Temporal tables overview — Microsoft SQL Server documentation](https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal/overview)
  — system-versioning maintained by the system rather than the application.
- [Graphiti bi-temporal model — getzep/graphiti documentation](https://mintlify.wiki/getzep/graphiti/concepts/temporal-model)
  — `valid_at` / `invalid_at` / `created_at` / `expired_at` on edges,
  invalidation-instead-of-deletion, `invalid_at` set to the new fact's
  `valid_at`, and the LLM-decided edge resolution this design departs from.
- [Graphiti: knowledge graph memory for an agentic world — Neo4j](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/)
  — the temporal-graph memory framing behind the Zep claims.
- Internal: `docs/research/2026-08-28-longmemeval-first-number.md` (the 0.154 /
  0.429 rows), `docs/research/2026-08-27-number-one-memory-market-research.md`
  (competitor claims, the audit caveat, and the "Don't Ask the LLM to Track
  Freshness" line).
