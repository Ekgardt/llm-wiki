# When we would drop the other tool

Dated 2026-09-12, written while the parity run of `benchmark/code-parity-v2.json`
was still executing and before any of its numbers were read. Issue #24, section
E, asks for exactly that order: a rule chosen after seeing the numbers is not a
rule, it is a rationalisation.

## What is being decided

Whether llm-wiki's own code-navigation surface can replace `codebase-memory-mcp`
in daily use — that is, whether the owner can uninstall the other tool without
losing an answer they have today.

## Sources

1. `docs/research/2026-08-31-a-decision-rule-stated-before-the-run.md` — the same
   discipline applied to the memory stand: repetition rather than a
   significance test, and an arm wins only by more than the baseline's own
   observed spread. Its threshold (0.035 accuracy, ≤ 1.5× tokens) is for
   LongMemEval and does not transfer; its *method* does.
2. `docs/research/2026-08-28-code-parity-first-pairing.md` and
   `docs/research/2026-08-29-code-parity-rerun-and-the-safety-claim.md` — the
   two earlier pairings on this stand. The first measured the product surface at
   0/13 and named one shared defect behind ten of them; the second is why the
   stand reports `wrong_but_confident` and `operator_attention_events`
   separately from correctness. Both established that a single run of this stand
   is a measurement, not a verdict.
3. `benchmark/run_code_parity.py`, module docstring — what each column actually
   is: `tokens` is `len(answer)//4`, grading is word-boundary term matching,
   wall time includes each side's real process start, and a timeout grades
   `wrong`.

## The rule

llm-wiki replaces `codebase-memory-mcp` only when all four hold on the same
task set, over **three runs** of `code-parity-v2.json` plus the cross-service
set:

1. **Correctness.** llm-wiki's mean `correct` count is greater than or equal to
   codebase-memory-mcp's, and on no individual task does llm-wiki grade `wrong`
   while the other side grades `correct` in every run. A single task the other
   tool answers and we never do is a reason to keep it installed, whatever the
   totals say.
2. **Safety.** `wrong_but_confident` is no higher than the other side's. A
   confident wrong answer costs more than a refusal, because the operator does
   not go looking.
3. **Attention.** `operator_attention_events` — calls that did not answer at all
   — is no higher than the other side's. This is the column that fails a tool in
   practice.
4. **Cost.** Mean tokens per answered task ≤ 1.5× the other side's, and p95 wall
   time ≤ 2× the other side's. The multipliers are the ones issue #24 names.

The column that counts on our side is the product as an agent reaches it. Where
`llm_wiki` and `llm_wiki_best` disagree, the *lower* of the two is our score
until the surface an agent actually calls is the better one, because a number
only the benchmark can reach is not a product. Both columns are published either
way; that is the rule from the 29 August rerun and it stays.

## What the rule does not decide

It does not decide whether to keep the other tool installed for a single
language or a single question shape — that is a narrower question and needs its
own evidence. And three runs of a sixteen-task set is a small sample; the rule
is a decision procedure, not a confidence interval, exactly as the field's own
practice is.

Files: `docs/research/2026-09-12-when-we-would-drop-the-other-tool.md`,
`benchmark/run_code_parity.py`.
