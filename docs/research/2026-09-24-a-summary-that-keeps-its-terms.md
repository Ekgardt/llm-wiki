# A summary that keeps its terms

Dated 2026-09-24. The stand's recall gate failed last night: on 29 real sessions
whose labels two automatic readings confirmed, the classifier's summary carried every
required term in 19 and lost exactly one in 10 (durable content recall 0.655, gate
0.8; `docs/research/2026-09-23-the-corpus-labels-itself.md`). The owner said «делай».

Files: `scripts/flush_memory.py`, `tests/test_flush_classification.py`,
`tests/test_flush_classification_benchmark.py`, `CHANGELOG.md`,
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`,
`docs/research/2026-09-24-a-summary-that-keeps-its-terms.md`.

## What was found

- The ten lost terms are all names and numbers: `sci-ledger`, `lme500.jsonl`,
  `run 35382495391`, `_posix_launch`, `record_set_digest`, `GenerationUnreadable`,
  `McNemar exact p = 0.5`, `LongMemEval multi-session 0.752`, `closed-day rule`,
  «шесть копий». None is a sentence; each is the kind of thing a later search types.
- The prompt asks for bullets that "fit on one line" under five topical sections and,
  since last night, to keep names as the transcript spells them. A one-line bullet
  about a decision has room for the decision, not for every identifier around it; the
  model keeps the ones it deems central and the third term goes.
- No consumer parses the section names (`grep` over `scripts/` finds them only in the
  prompt), so a section can be added without touching a reader. The body is written
  into the daily log as it stands and read by compile as prose.

## Practice on this date

- Entity-level consistency of abstractive summaries is measured as the precision and
  recall of the source's named entities in the summary, and models drop or invent
  entities unless the task makes entities an explicit target (Nan et al., "Entity-level
  Factual Consistency of Abstractive Text Summarization", EACL 2021, arXiv:2102.09130,
  fetched 2026-09-24). The stand's recall is that metric with the terms chosen by an
  independent reading.
- The cheapest way to make a term an explicit target of a generated summary is a
  dedicated slot for terms, bounded, verbatim, beside the prose — the same shape the
  rubric reading already uses (`phrases`, at most three, at most five words each).

## The decision

Add one section to the distilled block, for every tier that produces a body:

    - **Terms** — the identifiers, file paths, versions, run numbers, flags and
      commands a later search would type, exactly as the transcript spells them;
      at most eight (any tier).

Nothing else in the prompt changes; the grammar, the parser and every consumer are
untouched. The section costs at most a line of output per session. Measured on the
same 29 confirmed cases before this note is closed; if recall does not rise, the
change is reverted and this note says so.

## Sources

- Nan et al., "Entity-level Factual Consistency of Abstractive Text Summarization",
  arXiv:2102.09130 — https://arxiv.org/abs/2102.09130 — fetched 2026-09-24.
- `docs/research/2026-09-23-the-corpus-labels-itself.md`; the stand's report on the
  live corpus, 2026-09-23 night.

## Measured, 2026-09-24 morning: no change, reverted

The same 29 confirmed cases, the prompt with the `Terms` section: tier accuracy 1.0,
durable content recall 0.655 — ten misses again, each one term. Six of the ten are
the same sessions as last night's run; four moved, which puts the run-to-run noise of
this number near 0.1 at n = 29. A terms slot does not make the model choose the
rubric's three terms out of the session's dozens; it chooses its own eight. The
section is removed; the prompt ships as it was after 2026-09-23. `scripts/flush_memory.py`
and its tests are unchanged by this note.

What would move the number is not another sentence in the prompt. Either the marker
set is drawn from both readings (a term both name is one a summary must carry), or the
metric checks the terms against the whole daily-log record rather than one summary.
Both are measurement changes and need their own note; neither is a chore for a person.
