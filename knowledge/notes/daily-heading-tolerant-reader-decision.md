---
type: decision
status: accepted
confidence: high
source_authority: user
created: 2026-08-28
---

# Daily Heading: The Reader Widens, The History Stands

One-sentence summary: the claim reader accepts both the bare `# YYYY-MM-DD`
heading and the titled `# … — YYYY-MM-DD` one this vault has always written,
because widening the writer instead would leave every append-only daily log
already on disk unreadable.

## Decision

`claims._DATE_RE` admits exactly two anchored forms — the bare date heading
(the original contract) and a titled heading whose date ends the line. Both
anchor on the start of the line and terminate with the date, so nothing
ambiguous is admitted; every other heading still refuses by name with
`claim source has no canonical daily date`. `scripts/daily_log_append.py` is
unchanged and keeps writing the titled form.

## Why

Measured 2026-08-28: every daily log in this vault, back to the earliest
(`2026-04-13`), carries the titled heading, written by the one producer at
`daily_log_append.py:124` and `:158`. The bare form was never written here.
So the reader was written against a shape that does not exist, and the
consequence was total: zero claim ledgers, `cache/claims.sqlite3` absent —
the subsystem had never run (`NEW-120`).

Daily logs are append-only by contract, so a writer-side fix would only serve
future days and orphan the whole history. That is the second refusal this
vault already ruled against in `NEW-67`: an unaccounted form should leave
evidence findable, not invisible — for memory, invisibility is the worse
failure.

The tolerant-reader shape is right *here* specifically, and the modern
criticism of Postel's law does not apply: there is exactly one producer, in
this same repository, and it is not sloppy — its format is the operating one.
Tolerance is bounded to two anchored alternatives, not to "find a date
somewhere".

## Evidence

After the change the pipeline reads real vault bytes for the first time:
`2026-08-20.md` → 15 blocks, `2026-08-21.md` → 295, `2026-08-24.md` → 1068;
all nine daily logs accepted, `knowledge/daily/README.md` still refused, the
bare form still accepted, a heading with a date in the middle still refused.

## Source

- `docs/research/2026-08-28-which-daily-header-is-canonical.md`
- `scripts/claims.py` (`_DATE_RE`), `scripts/daily_log_append.py`
- `tests/test_daily_heading_reader.py`

## Related

- [[knowledge/notes/daily-entry-quote-anchor-decision]] — the timestamp
  addresses an entry and the quote proves it; the same split of address from
  proof is why widening the address form costs no rigour.
