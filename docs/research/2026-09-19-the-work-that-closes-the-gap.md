# The work that closes the gap

Dated 2026-09-19. The research before the plan the owner asked for: what to build so the
product stops losing the questions it loses, measured rather than argued.

## What the measurement says (the starting point)

From `/home/user/.claude/jobs/80be9db9/tmp/WHY-WE-LAG-2026-09-19.md`, computed over the 500
recorded rows of `cache/benchmarks/full-2026-09-18/`:

- 75 losses on 470 answerable questions: 26 refusals with the evidence in hand, 23 reader
  errors with full evidence, 14 questions whose session never entered the 12 candidates, 10
  where the session came but the needed turn did not, 2 provider errors.
- Multi-session carries 30 of the 75. On its failures, 6.05 of the 12 candidate slots went to
  1.95 sessions — three chunks of one session — while 1.3 needed sessions got none.
  Accuracy falls 0.886 → 0.611 as the number of needed sessions goes 2 → 4.
- 90 of 121 multi-session questions are counting or summing. The aggregation pass fires only
  when the reader declares a count itself: it fired on 69 of 121 (0.826 against 0.654), and
  18 of the 30 failures never fired it. Of 27 wrong counts, 13 are undercounts (10 with
  incomplete coverage) and 8 are overcounts (7 with complete coverage).
- The judge does not explain the gap: one clear judge error in 47 refusals, three arguable.

## Practice on this date

- **Coverage before diversity, and diversity before more of the same.** The recorded research
  of 2026-08-30 (`docs/research/2026-08-30-what-survives-into-context.md`) already states the
  rule and its sources: budgeted packing cast as monotone submodular maximisation — relevance,
  query coverage, representativeness and diversity together — beats an MMR baseline at equal
  token cost (arXiv 2607.00725); plain diversity alone takes a recall penalty, so the rule is
  "do not spend two slots on one source while another source has none", not "prefer different
  things". That change was made in the packer (`_fitted_selection`). **It was never made where
  the 12 candidates are chosen**, which is where the measurement now says the loss happens.
- **Diversify only when one hit is enough; keep relevance order when all hits are needed.**
  Chen and Karger (SIGIR 2006), recorded in `docs/research/2026-09-15-what-a-slot-should-reward.md`:
  diversifying raised 1-call@10 from 0.791 to 0.835 and lowered P@10 from 0.333 to 0.269. A
  counting question needs *all* the hits, so a per-session quota must cap repeats, never drop
  a needed session's only chunk.
- **The benchmark's own authors name this class.** LongMemEval (ICLR 2025, arXiv 2410.10813,
  fetched today) evaluates "information extraction, multi-session reasoning, temporal
  reasoning, knowledge updates and abstention", reports "commercial chat assistants and
  long-context LLMs showing a 30% accuracy drop on memorizing information across sustained
  interactions", and proposes session decomposition, fact-augmented key expansion and
  time-aware query expansion. Two of those three exist here; the third — decomposition of a
  question into its sessions before reading — is what multi-session counting needs.
- **Abstention is a calibration with two error directions, not a switch.** Recorded in
  `docs/research/2026-08-29-where-we-stand-against-the-field.md`: the field trains and
  measures abstention in both directions; ours is set to one and never measured against the
  other. 26 of our 75 losses are refusals with the gold string in the prompt.

## The decisions

1. **A per-session quota over the 12 candidates.** At most two chunks of one session while any
   needed session has none; the lane score keeps deciding order within that rule. Coverage
   first, then fill — the packer's rule, moved one step earlier. Measured against
   single-session accuracy, which is what a too-strong quota would break first.
2. **The aggregation pass is chosen by the question, not by the reader's self-report.** A
   counting or summing question enters the pass by its own shape; the reader no longer has to
   declare the derivation for it to run.
3. **Counting counts things, not mentions.** The pass deduplicates by the thing counted before
   it answers, which is what the 8 overcounts with complete coverage ask for.
4. **The refusal keeps its two directions and both are reported.** A refusal with the evidence
   in hand is a measured error of the same weight as a wrong answer, printed beside the
   accuracy, so tightening one direction can never hide a loss in the other.
5. **One clean measurement.** Every number above comes from a run whose code changed three
   times mid-flight, and whose question blocks coincide with those changes. Nothing in this
   plan is confirmed until one run of the frozen tree measures it.

## Cost, by rule 4

The quota and the aggregation trigger cost no extra provider call and no extra token: the same
12 slots, the same budget, a different filling. Deduplicating what is counted costs one pass
over the already-retrieved set. Reporting both refusal directions costs one field. The only
expensive item is the measurement itself: about nine hours of wall time and 500 provider calls
per full run, which the owner permits one at a time.

Files: `scripts/retrieval.py`, `scripts/aggregation_pass.py`, `scripts/query_memory.py`,
`benchmark/longmemeval_score.py`, `benchmark/longmemeval_vault.py`,
`docs/research/2026-09-19-the-work-that-closes-the-gap.md`.

## Sources

- [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory (arXiv 2410.10813)](https://arxiv.org/abs/2410.10813) — fetched 2026-09-19.
- [What Survives Into Context: budget-constrained multi-hop RAG and submodular evidence packing (arXiv 2607.00725)](https://arxiv.org/abs/2607.00725v1) — recorded 2026-08-30.
- Chen and Karger, SIGIR 2006, on diversifying when one relevant result suffices — recorded 2026-09-15.
