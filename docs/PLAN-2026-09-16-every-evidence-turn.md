# The plan to put every needed turn in front of the reader

Dated 2026-09-16. What we will do, in order, to raise "every evidence turn the question
needs reaches the reader" as close to 1 as the evidence allows, and what cannot be
promised. Every stage is accepted only on measurement, on at least two independent
evaluations.

## Where we stand (measured 2026-09-14/16, LongMemEval-S, 189 answerable questions)

| Metric | Value |
|---|---|
| every evidence turn inside the reader's 12 | 0.698 |
| inside 24 | 0.799 |
| evidence never inside the fused top 100 | 10% of questions |
| all-evidence@12 with the cross-encoder judging 30 instead of 10 | 0.762 (43 of 43 controls unbroken) |
| dense lane alone, the 18 worst questions | all 59 evidence turns within its top 300, 45 within 100 |
| rank fusion on those 18 | pushes 26 of 59 past 100 |
| assistant replies in the fused top 100 | 69% |
| evidence turns that are user turns | 94% (against a 50.4% base rate) |

## The honest ceiling

- A per-question guarantee is impossible without assumptions about the data (Barber,
  Candès, Ramdas, Tibshirani 2021). What can be enforced is a share: "in 95 % of questions
  the reader receives every evidence turn", calibrated on held-out labelled questions.
- Even with perfect evidence a strong reader answers LongMemEval-S correctly 82-92 % of the
  time, and the judge itself is about 3 % noisy. LoCoMo's own answer key is about 6 % wrong.
- Facts never written down, or only inferable across turns, cannot be guaranteed at all.

## The stages

0. **Re-measure the two claims this plan leans on** (the role prior and the price of a
   silent lane), on our own run data, before any code changes.
1. **Union of channels instead of rank fusion as a gate.** Each lane contributes its own
   depth; a turn one lane ranks first is not buried because the other lane never saw it.
   Order inside the union by one score that carries the lane ranks, the role prior as
   log-odds, the date window and the reranker score. Expected: the 45 of 59 turns the dense
   lane already holds return to the head at no query-time cost.
2. **More cross-encoder judgement, then make it cheap.** Depth 30 with separate quotas for
   user and assistant turns (measured +6.4 points at the reader's 12). Then int8 runtime and
   a small-model prefilter so the deeper judgement costs no more than today's 2.3 s.
3. **Deliver an evidence set, not twelve chunks.** A token budget instead of a chunk count,
   each hit carried with its neighbouring turns, and a conformal cutoff that sets the size
   per question for a 95 % target. When the set does not fit, the reader reads it in parts
   or the answer is marked "not certified", and that share is reported.
4. **Write-time keys and an exact ledger.** A bounded nightly pass extracts dated user facts
   and attaches them to their turns as extra keys (never replacing the text; the LongMemEval
   paper measured +9.4 % recall). Counts, sums, latest values and dates are answered from a
   ledger built by two independent extractions that are reconciled.
5. **A stronger first stage if the number still falls short**: bge-m3 dense plus sparse (MIT),
   measured far better on Russian than our current small model, at a nightly cost.
6. **A second search only for uncertified questions**, directed at what the first pass did not
   cover — the one part of Bayesian search theory that survives on text.
7. **Continuous certification.** Planted questions every night, an audited sample of real
   ones, and a published confidence bound; recalibration when the questions drift.

## What we are not doing, and why

- **Bayesian effort allocation over cells** as the ranking engine: measured on our own
  questions, no gain; a published bandit method already beats rank fusion by more.
- **A stopping rule with a per-question guarantee**: every finite-sample rule on text needs a
  random sample of the relevant set, which a live question does not have.
- **Reading the whole history, or a memory made only of extracted facts**: measured worse.
- **Scanning the whole vault with the cross-encoder per query**: cost grows with the vault.

## Evaluation that cannot be fitted

The 189 questions stay the tuning set; the remaining 311 of cleaned LongMemEval-S are held
out; LoCoMo and a Russian/English question set over the owner's own vault are the
independent checks. Coverage, set size, certified share, latency and tokens are reported
together, per question type.

Files: `docs/PLAN-2026-09-16-every-evidence-turn.md`.
