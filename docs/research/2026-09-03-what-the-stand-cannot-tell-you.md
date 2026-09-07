# What the stand cannot tell you at n=50

Dated 2026-09-03. Measured, not argued.

## The measurement

Three runs of **the same commit**, same sample, same seed, same settings:
n=50, seed 101, 40 candidates, answer budget 122 880.

| run | answered | correct | correct per question |
|---|---|---|---|
| 1 | 35 | 27 | 0.5400 |
| 2 | 37 | 28 | 0.5600 |
| 3 | 39 | 31 | 0.6200 |

Mean 0.5733. **Spread 0.08 — four correct answers, with nothing changed.**

## What this invalidates

Every per-change number taken at n=50 today. All of them are smaller than the
spread:

| change | measured delta | verdict |
|---|---|---|
| the model names the citation, we locate it | 0.5400 → 0.5600 | inside the noise |
| terms joined with OR | 0.5600 → 0.5800 | inside the noise |
| the calendar of what happened | 0.5800 → 0.5400 | inside the noise |

None of those three is evidence of anything, in either direction. I reported
each of them as a small movement with a caveat; the caveat was not strong
enough, and the honest statement is that the stand could not see them.

## What survives

**Defect counts, not scores.** These are not sampled quantities; they are
counts of a mechanism firing, and they moved out of the noise entirely:

- questions reaching the model with an empty evidence manifest: **3 of 50 → 0**
- replies discarded as unparseable or unciteable: **about 10 of 50 → 1 of 50**
- refusals reading "no claim survived its citation gates": **4 of 8 → 0**

**The distance from the baseline.** Three baseline runs at n=200 scored 0.2100,
0.2100 and 0.2150 — spread 0.005 — against roughly 0.57 now. That gap is an
order of magnitude larger than any spread measured at either size, and it does
not depend on separating today's changes from each other.

## Why the baseline looked so much tighter

Inference, not fact. The baseline answered 64 to 69 of 200 questions and got
about 43 right; most of its behaviour was decided by gates that failed
deterministically, so there was little room for the model to vary. Now that the
system answers most questions, the model's own variation dominates, and the
spread grew with it. If that is right, tightness at n=200 was a symptom of the
defect rather than a property of the stand, and it will not come back.

## What changes in how we work

1. **n=50 is a smoke test, not a measurement.** It can show that a mechanism
   fires or a defect is gone. It cannot rank two versions.
2. **A ranking claim needs n=200 and three runs**, which is what the decision
   rule already said and what I stopped doing when each run started costing
   three hours.
3. **Prefer a defect count to a score** wherever the change has one. "Three
   questions arrived with no evidence and now none do" is checkable and stable;
   "0.54 became 0.56" is not.

## Source / Evidence

- Runs: `spread/r2.judged.jsonl`, `spread/r3.judged.jsonl`,
  `calendar/r.judged.jsonl` — same commit `ed6789d`
- Baseline: `benchmark/longmemeval-baseline-n200-r{1,2,3}.json`
