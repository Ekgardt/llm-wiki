# A calendar of what happened

Dated 2026-09-03. Written before building one, because the last three changes
were repairs and this one is an addition.

## Why now

Temporal reasoning is our weakest category and the largest remaining bucket of
substantive refusals: measured on 50 questions this morning, four of nine.
The recorded reasons are not "I could not find it". They read like this one:

> The evidence documents a meeting with a tourist from Australia, described as
> met 'last Thursday' on the subway, in a conversation logged 2023-…

The answerer holds the phrase and the anchor and declines, because our contract
requires a claim to be carried by a cited span and no span states the resolved
date. Write-time date resolution, shipped earlier today, closes exactly that
gap for the phrases it covers. It does not give the memory a way to ask *what
happened on a date*, or *between two dates*, which is what the rest of the
category needs.

## What the field has

**Chronos** (2026) is the closest published system, and it is measured on the
benchmark we use — LongMemEval-S, the same 500 questions in the same six
categories. It keeps two indexes side by side: a *turn calendar* holding the
unstructured conversation, and an *event calendar* holding subject-verb-object
tuples with resolved ISO 8601 datetime ranges and entity aliases, extracted
from the dialogue. At query time it searches both with dense and sparse legs,
fuses by reciprocal rank, and reranks with a cross-encoder.

Its ablation is the reason to care: **the event calendar alone accounts for a
58.9% gain over the baseline, while every other component contributes between
15.5% and 22.3%.** No other single component in any system we have surveyed
comes close, and it is measured on our own benchmark rather than transferred
from another one.

The normalisation underneath it is not new. Rule-based resolution of temporal
expressions against a document's creation time is the standard established by
HeidelTime and SUTime and formalised as ISO-TimeML; the survey literature is
explicit that a relative expression needs an anchor such as the document
creation time. What 2026 adds is keeping the resolved result as a *retrievable
structure* rather than as an annotation nobody queries.

## What we already have, and what is missing

We have the turn calendar: daily logs, indexed into a corpus generation with
lexical, dense and graph legs, fused by RRF, with an optional cross-encoder
reranker. Since this morning we also resolve day-granularity dates in the
user's own turns at write time.

What is missing is the second index and the leg that queries it. A date written
into an entry is findable only if the question happens to contain that date as
a string. "Which book did I finish a week ago" contains no date at all.

## The shape that fits this system

Three constraints decide it.

**Every claim must cite bytes in a Markdown file under `knowledge/`.** So the
events cannot live only in a database; a database can index them, but the text
a citation resolves to has to be in the vault. That rules out the shape where
extraction output exists solely as rows.

**No LLM call per session.** Chronos extracts events with an LLM pipeline. Our
stand ingests forty to fifty sessions per question and two hundred questions per
run; an extraction call per session is ten thousand calls per run, which makes
the measurement impractical and violates the standing requirement to be sparing
with tokens. Rule-based extraction is weaker but affordable, and it is
deterministic, which the measurement needs.

**The question's own "now" is known.** Every question on this stand carries the
date it is asked. A relative expression in the *question* can be resolved by the
same arithmetic we already apply to entries — and that is what turns "a week
ago" into a lookup.

So: emit dated event lines into the entry at write time, in the vault, citable;
and add a retrieval leg that resolves dates in the question and matches them
against those lines.

## What this will not do

It will not extract subject-verb-object structure, and it will not build entity
aliases. Both are LLM work, and both are what would take this from a date index
to Chronos's event calendar. Whatever gain we measure will therefore be a
fraction of the published 58.9%, and claiming otherwise would be dishonest.

It will also not resolve anything below a day, or months and years, for the
reasons already recorded: elapsed-time estimates are where models are least
reliable, and "two months ago" has no single correct answer.

## Sources

- https://www.emergentmind.com/papers/2603.16862 — Chronos: temporal-aware
  conversational memory; event calendar, dual indexing, ablation on LongMemEval-S
- https://arxiv.org/html/2507.06450v1 — semantic parsing for end-to-end time
  normalization
- https://dl.acm.org/doi/10.1007/s10462-023-10400-y — time expression recognition
  and normalization, survey; the document-creation-time anchor
- HeidelTime and SUTime as the rule-based baseline, ISO-TimeML as the format
