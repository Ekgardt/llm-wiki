# The aggregation pass is chosen by the question, and a count counts things

Dated 2026-09-19. Items 2 and 3 of the plan in
`docs/research/2026-09-19-the-work-that-closes-the-gap.md`: the second door into the
counting pass, and what "the same thing" means when a count deduplicates.

Files: `scripts/aggregation_pass.py`, `scripts/query_memory.py`,
`tests/test_a_count_that_reached_the_edge_looks_again.py`,
`tests/test_a_counting_question_opens_the_pass_itself.py`,
`docs/research/2026-09-19-the-aggregation-pass-is-chosen-by-the-question.md`.

All figures below are recomputed here, read-only, from the 500 recorded rows of
`cache/benchmarks/full-2026-09-18/lme500.judged.jsonl`, with the product's own
`aggregation_pass` imported rather than copied.

## What is wrong today

`scripts/aggregation_pass.py` says it in its own docstring: "The signal is not a word in
the question. It is what the answer *declared it did* — `derivation` is `count` or
`sum`." `query_memory._second_look` therefore opens the pass only when the first answer
tagged a claim `count` or `sum`.

Recomputed on the recorded run: an aggregating claim appears in 79 of the 500 rows and in
74 of the 133 multi-session rows. The diagnosis of the same day
(`WHY-WE-LAG-2026-09-19`, §4) measured what that costs: the pass fired on 69 of 121
answerable multi-session questions, accuracy 0.826 where it fired against 0.654 where it
did not, and 18 of the 30 multi-session failures never fired it.

The four ids the diagnosis names show the shape of the defect. Three of them
(`9ee3ecd6`, `67e0d0f2`, `8e91e7d9`) were answered in a single generation with no
derivation at all — "You need a total of 300 points", gold 100; "the total number of
online courses", 12 against 20; "the total number of siblings", 1 against 4. The reader
did not say it was counting, so the safety net was never hung.

## Decision 1 — the question opens the pass, and the calendar keeps its own questions

`asks_to_aggregate(question)` is a regular expression over the question: *how many*,
*how much*, *how often*, *number of*, *total*, *average*, *in total*, *altogether*,
*combined*. No model is asked what kind of question this is, so the second door costs no
token at all — law 4. It is English lexis, which is why it **joins** the self-report
instead of replacing it: a question asked in another language still enters through the
derivation the answer declares.

The new door yields to one older one. `calendar_pass` owns claims whose derivation is
`difference` and spends the single allowed regeneration on date arithmetic; the question
shapes overlap ("How many days ago did I attend a networking event?"). So the
question-shaped door opens only when the answer declared no date difference. An answer
that declared a count or a sum still opens the pass exactly as before — nothing that
fires today stops firing.

Measured over the 500 recorded rows (the trigger reads only the question text, so this
is exact, not an estimate):

| rows | trigger fires | the recorded run opened the pass |
|---|---|---|
| all 500 | 233/500 = 0.466 | 79/500 = 0.158 |
| gold is a bare quantity (168) | 141/168 = 0.839 | 65/168 = 0.387 |
| gold carries a number or a number-word (206) | 164/206 = 0.796 | 73/206 = 0.354 |
| gold carries no number at all (294) | 69/294 = 0.235 | 6/294 = 0.020 |
| multi-session (133) | 113/133 = 0.850 | 74/133 = 0.556 |

"Gold is a bare quantity" is a label read from the gold answer, never from the question,
so it is independent of the trigger's own wording. On those 168 questions the trigger
reaches 0.839 where the self-report reached 0.387. All four named ids fire. Exactly three
rows of 500 opened the pass in the run and would not fire the trigger — "What is the
minimum amount I could get if I sold the vintage diamond necklace and the pocket watch?",
"What time did I reach the clinic on Monday?", "Which airline did I fly with the most in
March and April?" — and the self-report still carries all three, because the union keeps
the old door open.

### Which counting questions the trigger still misses, and why on purpose

Of the 206 rows whose gold carries a number, 42 do not fire. Reading them: about twenty
are "How long had I been X when Y" and "How old was I when Y" — gaps between two dates,
which `calendar_pass` owns and which the trigger must stay out of; seven ask for a
percentage or a price difference, a ratio of two figures rather than an enumeration, and
adding *percentage* and *difference* would spend the pass on them for no measured gain;
the rest are not quantity questions at all and only carry a number-word inside a prose
gold ("I'm looking back at our previous conversation about …"). I left all three groups
out deliberately. The one genuinely countable miss is `91b15a6e`, "What is the minimum
amount I could get if I sold the vintage diamond necklace and the antique pocket watch?"
— a sum, caught today by the answer's own `sum` derivation, which the union keeps.

The cost is real and must be said plainly. On 294 rows whose gold holds no number at all
the trigger still fires 69 times (0.235): "how much", "total" and "average" appear in
questions that want one fact. And on the recorded run the pass cost about 4 500 extra
prompt tokens and about 100 extra seconds per question where it fired (14 169 against
9 650 tokens, 204 against 104 seconds, `WHY-WE-LAG-2026-09-19` §4). Firing it on 233 rows
instead of 79 is roughly 154 more questions paying that — call it 0.7 M extra prompt
tokens and four extra hours on a 500-question run. Whether the accuracy the diagnosis
measured survives the wider trigger is exactly what only a fresh run can settle, and
whether the extra time is acceptable is the owner's to weigh, not mine.

## Decision 2 — what "the same thing" means, and what it is not

I read the counting questions and their golds in the recorded rows. They count
**things, not mentions of things**, and the thing is named by the question:
"How many weddings have I attended" (gold: three, named by the couples), "How many
bikes did I service or plan to service in March" (gold: 2), "How many different types of
citrus fruits" (gold: 3), "How many tanks do I currently have" (gold: 3). Where the
reader overcounted with complete coverage, it had enumerated *events about* one thing:
`a9f6b44c` listed four bike services — "Road bike serviced at Pedal Power", "Road bike
chain cleaned and lubricated", "Road bike chain planned to be cleaned", "Commuter bike
front tire planned to be replaced" — and answered four, where the question asks how many
**bikes**, and the answer is two.

So: **the same thing means the same instance of the kind the question counts.** The unit
is read from the question by `counted_kind` — the head noun of the phrase after *how
many* / *how much* / *number of* — and it is the unit the deduplication must use.

### The deterministic fold was built, measured, and rejected

The obvious cheap implementation is to fold mentions whose identity matches. I built
three variants and measured each over all 500 recorded rows: **A** keys a mention by its
words up to and including the counted kind; **B** adds every number and capitalised name
anywhere in the mention; **C** keys by the whole normalised mention.

| variant | wrong counts it changes | right answers it would break |
|---|---|---|
| A (up to the kind) | 2 of 22 — `a9f6b44c` 4 → 2, exactly the gold; `gpt4_731e37d7` 6 → 4, where the error is missing evidence and the merge is beside the point | 3 of 65 — `2788b940` 5→4, `21d02d0d` 2→1, `gpt4_a1b77f9c` 3→2 |
| B (kind plus names and numbers) | 0 | 1 — `21d02d0d` 2→1 |
| C (whole mention) | 0 | 0 |

The breakages say why no surface rule works. `21d02d0d` enumerates "5K fun run on March
5th" and "5K fun run on March 26th" — two runs, one description. `2788b940` has "Zumba
class - Tuesday 7:00 PM" and "Zumba class - Thursday 7:00 PM" — two classes.
`gpt4_a1b77f9c` has "2 weeks ('The Nightingale')" and "2 weeks ('The Power')" — two
books. Every discriminator sits *after* the counted noun, and a rule strong enough to
fold three services of one bike is strong enough to fold two Saturdays into one.
Variant C, the only safe one, changes nothing at all on 500 rows. A deterministic fold
is therefore not shipped, and this note records the measurement so nobody builds it
again.

### What is shipped instead: the existing clustering call, told what it is counting

One clustering call already exists and already costs nothing extra — it is made inside
the pass over the mentions the answer enumerated, over the already-retrieved set, with
no further retrieval. Three things change about it, all free:

1. **It is asked in the unit of the question.** The mentions block now opens with the
   kind: "Each group must name one and the same *bike*." Today the call is asked which
   mentions "name the same real-world thing", which is true of a bike and of a bike
   service alike.
2. **Like things are blocked together.** The set is cut into nines
   (`CLUSTER_SET_SIZE`), and the order decides which mentions share a call. It was a
   plain casefold sort; it is now the kind-anchored key of variant A — used only to
   *order and group* the call, never to merge. This is the cheap form of the blocking
   the module already cites (in-context clustering, SIGMOD 2025,
   https://arxiv.org/html/2506.02509v1): place the records of one entity next to each
   other and let the model's one grouping call decide.
3. **The counting rule names the unit too.** "Before stating a count or a total, list
   every instance **of the thing the question asks for** ... the count is the number of
   entries."

Proposal is deterministic, disposal stays with the model. That is the only split the
measurement supports: variant A's one true group was found by the deterministic rule,
and its three false groups were all cases a reader can see are different at a glance.

What only a fresh run can settle: whether naming the unit makes the model group three
services under one bike, and whether the wider trigger's 0.826-against-0.654 gap holds
when the pass fires on 234 rows instead of 79.

## Prior art already in the repository

- `docs/research/2026-09-15-what-a-slot-should-reward.md`, verbatim: "Chen and Karger
  (SIGIR 2006) show diversifying is the right greedy rule for 'at least one relevant
  item' and relevance order the right one for 'all of them'; on TREC Robust diversifying
  raised 1-call@10 0.791 → 0.835 and lowered P@10 0.333 → 0.269." A counting question is
  the second objective: it needs all the hits, which is why the pass widens and merges
  rather than diversifies, and why the dedup happens after the reading and not in the
  ranking.
- LongMemEval (arXiv 2410.10813), abstract fetched 2026-09-19, verbatim: the benchmark
  evaluates "information extraction, multi-session reasoning, temporal reasoning,
  knowledge updates, and abstention", and its own remedies are "session decomposition
  for value granularity, fact-augmented key expansion for indexing, and time-aware query
  expansion for refining the search scope". Time-aware query expansion is already here
  (`query_memory._with_dated_leg`); fact-augmented key expansion is the fan-out
  (`fan_out_queries`); the piece still missing is decomposing a question into its
  sessions before reading, which is item 1 of the plan and not this change.
- `docs/research/2026-09-07-what-would-actually-put-us-ahead.md` and
  `docs/research/2026-09-07-is-each-plan-item-the-best-known.md` carry the original
  measurement behind the pass: a count over everything it found was still wrong 4.0
  times a run "because 'Domino's' and 'Domino's Pizza' were two".

## Sources

- [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory (arXiv 2410.10813)](https://arxiv.org/abs/2410.10813) — abstract fetched 2026-09-19.
- [In-context clustering of records (SIGMOD 2025, arXiv 2506.02509)](https://arxiv.org/html/2506.02509v1) — already cited by `scripts/aggregation_pass.py`.
- `docs/research/2026-09-15-what-a-slot-should-reward.md` — Chen and Karger, SIGIR 2006.
- `cache/benchmarks/full-2026-09-18/lme500.judged.jsonl` — the 500 recorded rows, read-only.
