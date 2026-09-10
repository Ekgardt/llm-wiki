# The calendar does the arithmetic — 2026-09-08

Task 3 of `docs/TASKS-to-100-2026-09-08.md`: date questions were right
0.67 of the time in run 1 (16 of 24). Read one by one, the losses are not
one problem but three, and none of them is the model's memory.

## What went wrong, by case (run 1, `second-look-n200-seed101-r1.judged.jsonl`)

1. **The number was never computed.** "How many days ago did I attend a
   networking event?" — the answer names the event and its date
   (2022-03-09) and stops; gold is 26. "How many weeks ago did I receive
   the chandelier?" — the answer describes the chandelier. The model had
   both dates and did not subtract.
2. **A gate refused the subtraction.** "How many weeks ago did I attend the
   Nordstrom sale?" — `no claim survived its citation gates: a derived
   claim must cite the spans its inputs came from, and there is only one`.
   A difference between an event and the day the question is asked has
   exactly one span: the event. The other input is the question's own
   date, which is no span. `MINIMUM_DERIVATION_INPUTS = 2` was written for
   counts and sums (2026-09-05 note) and is wrong for this shape.
3. **A relative date was read as an exact day.** "Four weeks ago" resolved
   to one calendar day, the only evidence on that day was a file header,
   and the answer was `unsupported_time_scope`; the event was four days
   off. Five silences of this kind. The official judge itself tolerates
   ±1 day on day counts, and the question authors write "four weeks" for
   "about four weeks".

## What the field does

- LongMemEval (arXiv:2410.10813): time-aware query expansion — extracting
  the time range a question asks about and using it in retrieval — gives
  +6.8 to +11.3 points on temporal questions; their judge template for
  temporal questions says "do not penalize off-by-one errors for the
  number of days".
- OMEGA (https://omegamax.co/blog/number-one-on-longmemeval): temporal
  query expansion and a temporal-reasoning prompt were among the cheapest
  gains on the way to 0.954.
- Program-aided reasoning (PAL, arXiv:2211.10435): when a step is
  arithmetic, let code do it and let the model do the reading; the model's
  errors on date arithmetic are the classic case.
- Our own decision `a-fact-is-stored-with-its-date-decision.md`: the
  arithmetic is ours and the anchor is certain; no one is allowed to guess.

## Decision

1. **Inputs for a difference are dates.** The prompt asks that a claim with
   `derivation: difference` between dates put the dates in `inputs` as
   `YYYY-MM-DD`, and says the day the question is asked is one of them
   when the question asks "how long ago".
2. **The calendar computes.** After the first answer, for every difference
   claim whose inputs hold two dates — or one date and the question's own
   date — code computes the gap in days and weeks. When the claim's text
   does not state that figure (within one day), the answer is generated
   once more with the computed figures beside the question as data, in
   the same way entity groups are. At most one regeneration per question,
   shared with the second look for counts: a claim is a count or a
   difference, never both. New module `scripts/calendar_pass.py`.
3. **One span is enough for a difference.** `_require_derivation_inputs`
   accepts one citation when the derivation is `difference`; counts and
   sums keep two.
4. **A relative date is a neighbourhood.** The system prompt says a
   relative expression in the question is approximate and evidence within
   a few days of the resolved day is in scope unless the question says
   exactly. `query_with_dates` adds the days around a "N weeks ago"
   resolution (±3 days) so the lexical leg reaches them.

## Cost

One extra answer call only on questions whose first answer declared a
difference and did not state the computed figure; nothing on the rest.

## Rule of decision

One run of 200, seed 101, judged; kept if the gain over the second-look
baseline exceeds 0.035, with the temporal category reported beside it.

## Addendum 2026-09-09: the dated leg (task 8)

LongMemEval's time-aware query expansion — extract the time range the
question asks about and retrieve inside it — is worth +6.8 to +11.3 points
on temporal questions (arXiv:2410.10813, §5). Until now a daily entry
carried no `valid_from`, so the index's own `since`/`as_of` window could
not see it, and a question's dates reached retrieval only as words.

Decision: a daily entry's `valid_from` is the date in its file name (the
one date about an entry that is certain); a question whose expressions
resolve to dates runs one more search inside [earliest − 3, latest + 3]
days, and its rows join the first candidates by vote. No dates, no leg.
The neighbourhood stays, so "four weeks ago" is still about four weeks.
