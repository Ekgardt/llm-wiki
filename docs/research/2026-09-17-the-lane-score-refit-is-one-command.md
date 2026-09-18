# The lane-score refit is one command

Dated 2026-09-17. Third audit, retrieval M5 and L1: the `lane_score` constants were fitted
under conditions the product does not always run in, and the rerank blend is nominal. Neither
can be corrected without a measurement run, and runs need the owner's permission. What can be
prepared now is the refit itself, so that when a run is permitted it is one command.

Files: `benchmark/fit_lane_score.py`, `benchmark/longmemeval_vault.py`,
`tests/test_the_lane_score_refit_is_one_command.py`

## What was found

- `scripts/lane_score.py` ships seven constants from a logistic fit over 189 LongMemEval
  questions (2026-09-16). The audit's arithmetic: with the dense lane silent and the reranker
  not run, `USER_TURN = 2.5144` against `LEXICAL_RANK = -0.5071·ln(rank)` puts every user turn
  at lexical rank ≤ ~140 above an assistant turn at rank 1. Questions whose evidence is what
  the assistant said lose their evidence whenever retrieval is degraded.
- The fit that produced those constants was made by probe scripts in a job scratch directory
  (`lane_matrix.py`, `learned_eval.py`, …, named in the 2026-09-16 note) which are not in the
  repository. There is no command in the repository that reproduces or refits them.
- The stand's per-question record carries coverage and ranks in aggregate, but not the
  per-candidate lane matrix the fit needs: each candidate's lexical rank, dense rank, rerank
  score, whether it is a user turn, and whether the dataset flags it as evidence.
- `longmemeval_coverage` already knows how to decide the last of those: `evidence_turns`
  gives the windows of every turn labelled `has_answer`, and a candidate carries one when a
  window appears in its text.

## Practice on this date

- A model fitted on one operating condition and applied in another is the standard
  distribution-shift failure; the remedy is not a guessed constant but a refit over data that
  includes the conditions in question. Here that means a fit whose rows include candidates
  from degraded runs (dense silent, reranker not run), which is exactly what the silence flags
  in the feature set are for.
- Per-group reporting is what makes such a refit safe to accept: an aggregate gain can hide a
  loss on one question type, and LongMemEval's six answerable types are the groups that
  matter — the assistant-evidence type is the one the audit says is at risk.
- Cross-validation by question, not by candidate: candidates of one question are not
  independent, and a fold that splits them reports an optimistic number.
- No new dependency. `scikit-learn` is present only transitively through
  `sentence-transformers` and is not declared in `pyproject.toml`, so the fit uses numpy
  alone: L2-regularized logistic regression by gradient descent, deterministic, no random
  seed, no solver options to drift.

## The decision

- The stand records the lane matrix. `longmemeval_vault` adds `lane_matrix` to each
  question's result record: one row per retrieved candidate with `lexical_rank`,
  `dense_rank`, `rerank_score`, `user_turn` and `evidence`, the last from the dataset's own
  labels through `longmemeval_coverage`. It is bounded by the candidate limit the run already
  uses, and it is written whether or not the reranker ran, so a degraded run is fittable.
- `benchmark/fit_lane_score.py` is the one command. It reads one or more result files
  (JSONL as the runner writes them, or a JSON array), fits the seven constants, and prints
  them ready to paste into `scripts/lane_score.py`, together with, per question type and
  overall: the share of questions whose evidence is all inside the reader's twelve under the
  shipped constants and under the refit, and the count of questions won and lost.
- It refuses to print constants it cannot defend: a fit needs labelled evidence rows and at
  least one question of each type present in the file is reported as such. It changes no
  shipped constant by itself — the operator pastes them after reading the per-type table.
- It runs no provider call, loads no model, and starts no benchmark. It reads a file.
- The refit itself waits for a permitted run. That is the only thing left, and it is the
  owner's to allow.
