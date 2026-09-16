# One score over the lanes

Dated 2026-09-16. The research behind replacing rank fusion as the last word on the order
with one score that carries what each lane said, what it did not say, and what kind of turn
a candidate is.

## What we measured on our own runs

LongMemEval-S, 189 answerable questions, the product's own retrieval, each question's
candidates recorded with their lexical rank, dense rank and role
(`cache/benchmarks/full-2026-09-14/`, probes in the job scratch):

- The union of both lanes at depth 300 contains **every evidence turn for 188 of 189
  questions**. Perfect ordering inside that pool would put all evidence in the reader's
  twelve for **0.995** of questions. The whole gap is ordering, not finding.
- Today's fused order delivers **0.698**.
- **91 % of evidence rows are user turns** against a 40 % base rate among candidates.
- A hand-set role weight on top of the fused order gives 0.767 cross-validated (23 questions
  won, 8 lost); one logistic score over lexical rank, dense rank, the two silence flags and
  the role gives **0.794** cross-validated (22 won, 4 lost). Its coefficients: role +1.29,
  lexical rank -1.24, dense rank -1.05, **lexical silence +0.35** — a lane's silence is not
  evidence against a candidate.
- The cross-encoder judging 30 candidates instead of 10 gives 0.762 on its own, with 43 of 43
  control questions unbroken (2026-09-15 measurement).

## Practice on this date

- **Reciprocal rank fusion is a voting rule, not a probability.** Cormack, Clarke and
  Buettcher (SIGIR 2009) define it over full permutations of the collection; a truncated list
  is outside its model, and a candidate one lane never returned is treated as if that lane had
  ranked it last ([RRF](https://dl.acm.org/doi/10.1145/1571941.1572114)).
- **Combining lanes by evidence, not by votes, is older than RRF.** Bayes-fuse (Aslam and
  Montague, SIGIR 2001) sums log-likelihood ratios per system, including the ratio for "this
  system did not retrieve it", which is exactly the quantity our measurement puts at slightly
  positive ([Bayes-fuse](https://dl.acm.org/doi/10.1145/383952.384007)).
- **The probability ranking principle is optimal when the score is a calibrated probability
  of relevance**, which a learned score over lane evidence approximates and a rank-vote does
  not ([PRP](https://link.springer.com/rwe/10.1007/978-0-387-39940-9_930)).
- **Learned fusion beats rank fusion in current work**: ORE (SIGIR 2025) reports +17.12 %
  Recall@100 over RRF on TREC-DL 2021 by reallocating effort between lanes
  ([ORE](https://dl.acm.org/doi/10.1145/3726302.3730073)).
- **A prior on where evidence lives is Koopman's first step**, and it pays when used as a
  weight rather than a filter: the same role signal as a hard filter cost us 9 points.
- **Simple, few-parameter models are the right size here.** We have 189 labelled questions;
  a five-feature logistic model with cross-validation is at the edge of what that supports,
  and the reranker score stays a separate later stage rather than another parameter.

## The decision

- The final order inside the candidate pool comes from one score over: the lexical rank, the
  dense rank, one flag per lane for "this lane never returned it", the role of the turn, and
  (where the stage ran) the cross-encoder score. Weights are fitted offline on a labelled
  calibration set, versioned, and shipped as constants; nothing is trained at query time.
- The candidate pool becomes the union of the lanes' own depths rather than the fused
  prefix, so a candidate one lane ranks first is never dropped because the other lane is
  silent.
- The measurement that accepts it: all-evidence-in-twelve, cross-validated, on LongMemEval
  plus a held-out split, with per-type figures and the loss count reported.

Files: `scripts/retrieval.py`, `docs/research/2026-09-16-one-score-over-the-lanes.md`,
`scripts/lane_score.py`, probes in the job scratch (`lane_matrix.py`, `union_eval.py`, `learned_eval.py`,
`combined_probe.py`, `combined_eval.py`).
