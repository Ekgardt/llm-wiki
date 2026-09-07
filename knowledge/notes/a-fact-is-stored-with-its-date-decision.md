---
type: decision
status: active
confidence: high
source_authority: ai-derived
date: 2026-09-03
---

# A Fact Is Stored With Its Date

One-sentence summary: relative dates the user states are resolved at write time
against the entry's own day and written into the entry as a dated calendar, and
a question is expanded with the dates it only implies so that the calendar can
be reached.

## Context

A session says "I met her last Thursday". The daily entry kept those words and
the day it was captured, and nothing ever joined the two. Asked later which day
that was, the answerer held both halves and refused, because a claim must be
carried by a cited span and no span stated the resolved date. The arithmetic was
trivial and no one was allowed to do it.

Measured on the LongMemEval stand 2026-09-03: of nine substantive refusals in
fifty questions, **four were this**, and temporal reasoning is the weakest
category we have. The recorded reasons name the phrase and the capture timestamp
in one sentence before declining.

## Decision

**The join happens at write time.** `append_daily` appends, for each relative
date the user stated, the day it resolves to and the sentence it was said in,
sorted by date. It is Markdown inside the entry, because every published claim
must cite bytes in the vault; a row in a database cannot be cited.

**The question is expanded with its own dates.** "Which book did I finish a week
ago" carries no date, so nothing dated could match it. The same arithmetic dates
the question, anchored on the day it is asked — a date stated in the question
where there is one, today otherwise.

## Three deliberate narrowings

**Nothing below a day.** "Last night", "this morning", "a few hours ago" are
exactly where an elapsed-time estimate fails, and none is resolved.

**Only the user's own turns.** A model writes "last night" for an hour ago. Our
arithmetic does not depend on any model's estimate — the anchor is the entry's
day — but a phrase the assistant wrote can be wrong at the source, and resolving
it would turn a loose remark into a dated fact. The user's account of their own
week is the authority this vault already ranks highest, and a wrong date the
user gave is their record rather than our invention.

**No months or years.** "Two months ago" has no single correct answer, and a
confident wrong date is worse than no date.

## What was not built

Chronos's event calendar stores subject-verb-object tuples with datetime ranges
and entity aliases, extracted by an LLM pipeline, and its ablation credits that
module alone with a 58.9% gain over baseline on LongMemEval-S — the benchmark we
use. We built the date index and not the extraction: forty to fifty sessions per
question and two hundred questions per run make one extraction call per session
ten thousand calls per measurement, which is neither affordable under the
token-sparing requirement nor deterministic enough to measure against.

**So the published 58.9% does not transfer to us and must not be quoted as
ours.**

## Source / Evidence

- `docs/research/2026-09-03-a-calendar-of-what-happened.md`
- `scripts/temporal_anchor.py` — `resolutions`, `events`, `annotation`,
  `query_with_dates`, `spoken_by_the_user`
- `scripts/flush_memory.py` — `_dated_block`
- `scripts/query_memory.py` — `searchable_question`, `_asked_on`
- `tests/test_a_fact_is_stored_with_its_date.py`

## Links

- [[knowledge/notes/session-evidence-retention-decision]] — what the entry holds
  that this dates.
- [[knowledge/notes/daily-entry-quote-anchor-decision]] — how evidence binds to
  the entry that contains it.
- [[knowledge/notes/a-question-is-not-a-conjunction-decision]] — why the
  expanded query can match at all.
