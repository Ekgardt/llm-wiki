# Sixteen of sixteen

Dated 2026-09-12, after the three changes the owner approved
(`docs/research/2026-09-12-three-changes-to-pass-them.md`) and the two defect
fixes before them. Three runs of `benchmark/code-parity-v2.json` and three of
the cross-service pair, both sides indexing the same checkout, graded by the
rule written before any number was read.

## The numbers, three runs each

| side | correct (of 16) | tokens | wall | p95 per task | confident-wrong | non-answers |
|---|---|---|---|---|---|---|
| `llm_wiki` | 16, 16, 16 | 8 836 | 63.2 / 62.9 / 63.5 s | 10.98 s | 0 | 0 |
| `llm_wiki_best` | 16, 16, 16 | 6 648 | 49.9 / 50.1 / 49.2 s | 5.45 s | 0 | 0 |
| `cbm` | 15, 15, 15 | 5 632 | 35.4 / 35.2 / 34.9 s | 4.01 s | 1 | 0 |

Cross-service, three runs: X01 ours in 0.61–0.64 s and **correct** against their
**partial** in 1.95–2.22 s; X02 wrong on both sides, which is the boundary the
2026-09-11 decision drew on purpose.

Where we were before this work, for the same tasks and the same machine:
13 correct, 9 869 tokens, p95 12.70 s, one confident-wrong answer.

## Against the rule, condition by condition

Taking `llm_wiki` as our score, because that is the surface an agent reaches:

1. **Correctness — passes.** 16 against 15 in every run, and there is no longer
   any task the other tool answers and we do not. The task we used to lose in
   every run was the constant; the task they lose in every run is "which tests
   exercise this function".
2. **Safety — passes.** Zero confident-wrong answers against their one, in every
   run.
3. **Attention — passes.** Neither side ever failed to answer.
4. **Cost — still fails on the default surface.** Tokens 1.57× against a 1.5×
   ceiling and p95 2.74× against a 2× ceiling. On `llm_wiki_best` the same two
   are 1.18× and 1.36×, so the better surface passes all four and the default
   one does not.

**So the rule is not yet satisfied, and I am not restating it to make it so.**
Three of four conditions are met on both surfaces; the fourth is met on one of
them. The other tool stays installed until the default surface passes too.

## What the remaining gap is made of, measured

- **p95 10.98 s is two tasks.** T06 and T07 both ask `find_dead_code`, and both
  take about 11 s while every other call is now 2.3–5.4 s. Nothing else is
  within 5 s of them.
- **Tokens: 2 156 of 8 836 are one answer.** The architecture summary lists 110
  entry points, and every row repeats the absolute prefix
  `/home/user/llm-wiki-tasks/` — about a quarter of that answer is the same 25
  characters said 110 times, in an answer whose top level already names the
  directory.

Both are the same kind of finding as the ones already fixed: waste, not
capability. Neither needs a new decision — they continue the cost work the owner
already approved — and both are measured above rather than assumed.

## Sources

`benchmark/code-parity-v2-2026-09-12-after-run{1,2,3}.json`,
`benchmark/code-parity-cross-service-2026-09-12-after-run{1,2,3}.json`,
the three `…-v2-2026-09-12-run{1,2,3}.json` files from before the work, and
`docs/research/2026-09-12-when-we-would-drop-the-other-tool.md` for the rule.

Files: `benchmark/run_code_parity.py`,
`docs/research/2026-09-12-sixteen-of-sixteen.md`.
