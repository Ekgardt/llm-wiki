# One session does not take the window

Dated 2026-09-19. Item 1 of the plan in
`docs/research/2026-09-19-the-work-that-closes-the-gap.md`: a per-source quota over the
twelve candidates the reader sees, and what the recorded rows can and cannot say about it.

Files: `scripts/retrieval.py`, `tests/test_one_source_does_not_take_the_window.py`,
`benchmark/longmemeval_vault.py`, `tests/test_the_lane_score_refit_is_one_command.py`,
`docs/research/2026-09-19-one-session-does-not-take-the-window.md`.

## What was found in the code

- The visible order is `_visible_order` → `_capped`: page diversity, then the lane score
  over episodes, then the exact-filename promotion, then the window of twelve.
- `_page_diverse` gives a compiled page one slot and sends its later chunks behind the
  distinct pages. It deliberately does **not** do this for `knowledge/daily/**` and
  `knowledge/raw/**`: `_place_by_page` returns early for an episode, so every turn keeps
  its rank (decided 2026-09-15).
- `_evidence_ordered` then sorts the episodic positions by `lane_score.candidate_score`.
  For a corpus that is all episodes — every LongMemEval run — this replaces whatever order
  `_page_diverse` produced. **So there is no per-session rule at all in the candidate
  selection today.** Any quota has to be applied after the lane sort or the sort erases it.
- The packer already carries the same rule one step later. `query_memory._shed_one` drops
  "the second span from a page already present ... before the only span from another page",
  and its unit is `_entry_key` = `(source_path, heading_ancestry)`, because a daily file
  holds every session of its day. That is the identity the candidates already carry:
  `RetrievalCandidate.relative_path` plus `heading_path`.

## The measurement this answers

From the failure classification of the 2026-09-18 run, computed over its 500 recorded
rows in `cache/benchmarks/full-2026-09-18/`: on the multi-session questions that lose part
of their coverage, 6.05 of the 12 slots go to 1.95 sessions while 1.3 needed sessions get
none, and accuracy falls 0.886 → 0.611 as the number of needed sessions goes 2 → 4.

## The contrary measurement, which is ours and on point

`docs/research/2026-09-15-what-a-slot-should-reward.md`, 189 LongMemEval questions with the
product's own ranking, swapping only the slot rule:

| rule | all turns @12 | all sessions @12 |
|---|---|---|
| at most 2 per session | 0.566 | 0.952 |
| at most 3 per session | 0.608 | 0.942 |
| relevance order for episodes (today) | **0.698** | 0.910 |

A cap buys session coverage and pays for it in turn coverage, and turn coverage is the
quantity that tracked accuracy when the lane score was fitted (0.698 → 0.815,
`docs/research/2026-09-16-one-score-over-the-lanes.md`). Two things separate that
measurement from this change and neither makes it safe to ignore: it was taken over the
pre-lane-score order, and it capped unconditionally where this rule defers only while
another source has nothing. It is the risk this change carries, and it is why the cap is
one named constant.

## What the recorded rows can bound, and what they cannot

The run recorded the **capped window only** — twelve `lane_matrix` rows — and no row
carries the identity of its source. A candidate-by-candidate replay is therefore
impossible: the chunks a freed slot would admit were never written down. Two exact bounds
can be computed instead, because every `evidence` row belongs to a labelled answer session
and `coverage.sessions_covered` says how many labelled sessions hold a row at all:

- **floor on the loss** — a cap of two per source demotes at least
  `evidence_rows − 2 × sessions_covered` evidence rows out of the window;
- **ceiling on the gain** — a question can gain a needed source only when
  `sessions_covered < sessions_labelled`, and the quota provably frees a slot there only
  when `answer_sessions_retrieved > 2 × sessions_covered`.

Replayed over `cache/benchmarks/full-2026-09-18/lme500.judged.jsonl`, 470 answerable rows,
cap 2:

| type | n | missing a needed source | can gain one | slots freed | loses evidence | rows demoted | of which judged correct |
|---|---:|---:|---:|---:|---:|---:|---:|
| knowledge-update | 72 | 0 | 0 | 376 | 0 | 0 | 0 |
| multi-session | 121 | 19 | 12 | 447 | 0 | 0 | 0 |
| single-session-assistant | 56 | 1 | 0 | 214 | 6 | 13 | 4 |
| single-session-preference | 30 | 2 | 0 | 84 | 2 | 2 | 2 |
| single-session-user | 64 | 0 | 0 | 252 | 0 | 0 | 0 |
| temporal-reasoning | 127 | 19 | 9 | 465 | 0 | 0 | 0 |
| **all** | **470** | **41** | **21** | **1838** | **8** | **15** | **6** |

At cap 3 the same replay gives 12 questions that can gain and 6 that lose (8 rows).

Read plainly: at most 21 of 470 questions can gain a needed source, at least 8 lose an
evidence row they had and 6 of those 8 are answers the judge scored correct today. The
1838 freed slots are 3.9 of the 12 slots on the average question — the change is large,
and most freed slots go to sources that hold nothing the question needs. The ceiling is a
ceiling and the floor is a floor; the difference between them is exactly what one run of
the frozen tree measures and nothing else can.

## The condition, and why it changes less than it should

The rule is conditional: a chunk waits only while some source the pool holds has no place.
The table above is what an **unconditional** cap does. The question is whether the
condition ever lifts inside the window of twelve, and the answer decides whether the loss
above is real.

It lifts only when the window already holds every source the pool holds. The pool is
`_candidate_pool(12)` = 96 chunks. Measured over the 500 recorded rows, a question's
corpus holds 497.7 chunks over 47.8 sessions on average — **10.45 chunks per session** —
so 96 chunks cannot come from fewer than about nine sources unless the longest sessions are
several times the average. The window holds at most twelve, and it holds far fewer in
practice: the labelled sessions alone took 6.05 of the 12 slots over 1.95 sessions on the
multi-session questions that lose.

Counting only the questions where the window provably cannot hold as many sources as the
pool must contain — `12 − answer_sessions_retrieved + sessions_covered <
ceil(96 / biggest)`, with `biggest` a generous multiple of the average session:

| longest session | quota must bind | can gain (of 41 missing) | loses evidence | rows demoted | of which judged correct |
|---|---:|---:|---:|---:|---:|
| 1 × average | 369 of 470 | 20 | 7 | 14 | 5 |
| 2 × average | 158 of 470 | 8 | 3 | 8 | 1 |
| 4 × average | 56 of 470 | 0 | 1 | 3 | 1 |

**The condition does not remove the loss.** Under the assumption nearest the measured
corpus — sessions of about the average length — the quota must bind on 369 of 470
questions and the floor is 7 questions and 14 rows, against 8 and 15 for the
unconditional cap. What the condition changes is the *provability*, not the behaviour: on
the questions counted out the rows simply do not say whether a source was starved.

The condition is still worth having, and not only on paper. Outside this benchmark the
product's pools are small — a vault query over a handful of compiled pages routinely
retrieves every source it holds — and there the quota is inert by construction, where an
unconditional cap would reorder for nothing.

## Practice on this date

- **Coverage first, then fill.** Budgeted packing as monotone submodular maximisation beats
  an MMR baseline at equal token cost ([What Survives Into Context, arXiv
  2607.00725](https://arxiv.org/html/2607.00725)), recorded here 2026-08-30 and already
  implemented in `query_memory._shed_one`. The rule is "do not spend two slots on one
  source while another source has none", not "prefer different things".
- **Diversify only when one hit is enough.** Chen and Karger (SIGIR 2006): diversifying
  raised 1-call@10 0.791 → 0.835 and lowered P@10 0.333 → 0.269. A counting question needs
  every hit, so a quota may cap repeats and must never drop a source's only chunk.
- **The benchmark's authors name this class.** LongMemEval reports "commercial chat
  assistants and long-context LLMs showing a 30% accuracy drop on memorizing information
  across sustained interactions" and proposes "session decomposition for value
  granularity, fact-augmented key expansion for indexing, and time-aware query expansion
  for refining the search scope" ([arXiv 2410.10813](https://arxiv.org/abs/2410.10813),
  fetched 2026-09-19).
- **Where a cap is kept, it demotes rather than drops** (Vertex AI Search `CrowdingSpec`,
  recorded 2026-09-15; the page was not reachable today, so it is cited as recorded, not
  as fetched). Google shows at most two results per site except where its systems judge a
  result especially relevant.

## The decision

- A per-source quota is applied to the ordered candidates after the lane sort and before
  the exact-filename promotion. The source is the entry the packer already keys on:
  `(relative_path, heading_path)`.
- At most `SOURCE_QUOTA = 2` chunks of one source keep their place **while a source the
  pool holds has no place at all**. The condition is the rule, not a decoration on it: an
  unconditional cap pays the 2026-09-15 price on every question, including the ones where
  nothing is starved. A source's first chunk is always kept, so having been seen and
  having a place are the same thing and the condition is `len(seen) < len(sources)`.
- Chunks over the quota are **deferred to the back of the same list**, never dropped, in
  their own order. `_capped` still applies the window last.
- A source with a chunk already waiting keeps its later chunks behind it too. Without that,
  a slot freed by demoting a source's third chunk could be spent on that same source's
  eighth — a worse chunk in the place of a better one — and a source would overtake itself.
- Four properties follow and each is pinned by a test: a pool whose sources all hold a
  place is untouched; a pool holding one source is untouched; a source's first chunk is
  never deferred, so an only chunk cannot be displaced; a source's chunks keep their order
  relative to each other.
- The cap is one constant so the fresh run can measure 2 against 3 without a redesign.
- `benchmark/longmemeval_vault.py::_lane_row` now records each candidate's
  `(path, heading_ancestry)` as `source`. One field, and the next run's `lane_matrix`
  becomes replayable candidate by candidate instead of bounded.
- Not settled here, and only a full run of the frozen tree can settle it: whether the
  gain ceiling or the loss floor is nearer the truth, and therefore whether the cap belongs
  at 2, at 3, or conditioned on the question's shape. The recorded rows say the loss is
  real and small and the gain is possible and unmeasured; they cannot say which is bigger.

## Sources

- [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory (arXiv 2410.10813)](https://arxiv.org/abs/2410.10813) — fetched 2026-09-19.
- [What Survives Into Context: budget-constrained multi-hop RAG and submodular evidence packing (arXiv 2607.00725)](https://arxiv.org/html/2607.00725) — recorded 2026-08-30.
- Chen and Karger, SIGIR 2006, on diversifying when one relevant result suffices — recorded 2026-09-15 in `docs/research/2026-09-15-what-a-slot-should-reward.md`.
- `docs/research/2026-09-19-the-work-that-closes-the-gap.md` — the plan this implements.
