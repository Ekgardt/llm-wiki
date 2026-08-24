# The quote is the anchor, the timestamp is the address

Date: 2026-08-24
Reason: the imported history produced 126 durable items in the daily log, and not
one of them can be compiled into a page. The compile refuses with `compile
evidence timestamp block is ambiguous or missing`, because seventeen entries
written today share three timestamps, and the boundary decision requires that
exactly one entry declare the timestamp evidence names.

## What is measured here

`knowledge/daily/2026-08-24.md` holds 358 entries. Twelve of them carry
`[13:25:41]`, three carry `[13:24:10]`, two carry `[13:22:53]` — the
consolidation batches of one day shared a single moment. That writer is fixed, so
no new collisions appear, but the daily log is append-only: the entries already
written stay as they are, and every compile of that day fails whenever the model
cites one of them.

The rule that refuses is not about evidence at all. The timestamp is how the
evidence points at an entry; what proves the evidence is the quote, which must
still be a complete line and must still occur exactly once inside the entry.
Refusing on a repeated address discards a valid proof.

## What the practice says

The W3C Web Annotation model separates exactly these two things. A
`TextPositionSelector` says *where* in a document a selection sits; a
`TextQuoteSelector` says *what* the selection says, as exact text with prefix and
suffix. The practice is explicit that position is the fragile half and the quote
is the durable one: implementations write both and re-anchor by quote when the
position no longer matches, and the quote selector is described as sufficient in
the vast majority of cases.

The one case the practice names as needing more than a quote is the opposite of
this one: a document that repeats the same passage, where the quote alone cannot
say which occurrence was meant. There the position disambiguates the quote; here
the quote disambiguates the position.

## What follows for this repository

Evidence still names a timestamp, and the timestamp still selects the entries to
look in. When several entries declare it, the quote decides:

* exactly one of those entries contains the quote exactly once → that is the
  entry, and every existing check runs against it unchanged;
* no entry contains it, or more than one does → refused, as before.

Nothing else moves. The quote must still be a complete line with its bullet
prefix stripped; the span is still bound by digest to the immutable snapshot; a
timestamp no entry declares is still refused. What stops being refused is a valid
proof whose address happens to be shared — and what is still refused is a proof
that cannot be located unambiguously, which is the case the practice says
position must resolve.

This conflicts with one clause of the 2026-08-21 boundary decision ("exactly one
entry must declare it; two entries are refused"), so that decision is superseded
by a page that restates the boundary in full with this refinement, rather than
edited in place.

## Sources

- [Web Annotation Data Model, W3C](https://www.w3.org/annotation/) — the selector vocabulary that separates position from quote.
- [W3C selectors in practice, Semiont](https://github.com/The-AI-Alliance/semiont/blob/main/docs/protocol/W3C-SELECTORS.md) — position and quote written together; the quote is the durable anchor, position is re-anchored on save.
- [anchor-quote, Robert Knight](https://github.com/robertknight/anchor-quote) — a quote-selector anchoring library: quote first, position as a hint.
- [Notes for an annotation SDK, Jon Udell](https://blog.jonudell.net/2021/09/03/notes-for-an-annotation-sdk/) — the quote selector suffices in the vast majority of cases; repetition is the exception that needs position.
