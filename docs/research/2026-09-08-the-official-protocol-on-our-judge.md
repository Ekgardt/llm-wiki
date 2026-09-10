# Comparing with everyone on their protocol — 2026-09-08

## The question

The owner asked whether the competitors publish their test sets and stands,
and then asked for our stand to be reworked so the comparison is real.

## What is published (checked 2026-09-08)

| System | Stand and data | Judge | Per-question results | Licence |
|---|---|---|---|---|
| LongMemEval authors | `xiaowu0162/longmemeval`: 500 questions, `evaluate_qa.py`, hypothesis file `{question_id, hypothesis}`; V2 adds a harness and leaderboard (LAFS) | GPT-4o, temperature 0, 10 tokens, `'yes' in reply.lower()` | log with `autoeval_label` | MIT |
| Mem0 | `mem0ai/memory-benchmarks`: LoCoMo, LongMemEval, BEAM; ingest → search → evaluate; provider swapped by config | GPT-4o default; `--judge-model` accepts Anthropic | `results/platform/`, `results/oss/` | Apache-2.0 |
| Supermemory | MemoryBench in their repository: Supermemory, Mem0, Zep on one stand | GPT-4o | claimed | open |
| OMEGA | memory code only (`omega-memory/core`); no evaluation scripts or rows found; 95.4% is the mean of per-type means, the plain mean is 466/500 = 93.2% | GPT-4.1 | no | Apache-2.0 |
| Mastra | memory code only (`@mastra/memory`); the stand is not published separately | gpt-5-mini | no | open |
| Zep | experiment notebooks | GPT-4o | partly | — |
| Omi, ProsusAI MemEval | own stands with results (Omi: LoCoMo 0.866, LongMemEval 0.833) | — | yes | open |

Sources: https://github.com/xiaowu0162/longmemeval, https://github.com/xiaowu0162/LongMemEval-V2/,
https://github.com/mem0ai/memory-benchmarks, https://supermemory.ai/research/longmembench/,
https://github.com/omega-memory/core, https://omegamax.co/benchmarks,
https://mastra.ai/research/observational-memory, https://github.com/BasedHardware/omi-memory-benchmarks,
https://github.com/ProsusAI/MemEval.

## Where our number was not their number

Our judge asked "does the model answer state the same fact as the gold
answer" with one generic prompt, a rubric prompt for preferences, and graded
only answered non-abstention rows; abstentions were scored as silence-or-not.
The authors ask one templated yes/no per question type, grade abstention
questions on whether the model *said* it could not answer, parse the label
as `'yes' in reply.lower()`, and average over every question. Two different
metrics, both called accuracy.

The judge model differs too, and cannot be removed: every published figure
is GPT-4o (OMEGA GPT-4.1, Mastra gpt-5-mini), and this vault's rule is no
paid API beyond the subscriptions it has. Same-family judges are known to be
lenient; the report names the judge beside every number.

## Decision

1. `benchmark/longmemeval_official.py` carries the authors' templates
   character for character, their label parse, the abstention rule by
   question id, and both overall figures the field publishes — the plain
   mean and the mean of per-type means — so a reader can tell 95.4% from
   93.2%.
2. `longmemeval_judge.py --protocol official` grades every row, including
   abstention questions and our silences (rendered as the sentence the
   product shows a person), on our Claude judge, and writes
   `<results>.official.jsonl` and `<results>.official-report.json` with
   the judge named. The existing protocol stays as it was, so past runs
   remain comparable with each other.
3. `benchmark/longmemeval_hypotheses.py` writes the authors' hypothesis
   file, so anyone with an OpenAI key can grade our answers with the real
   `evaluate_qa.py` and GPT-4o.
4. Full 500 questions are one flag away (`--full`); the three 200-question
   runs finish first, as the owner ordered.

Not done: an adapter into Mem0's `memory-benchmarks`. Its answering head
and judge are OpenAI models; without a key the adapter could not be run
here, and rule 3 forbids shipping what was not verified. The hypothesis
file is the bridge until then.

Temperature 0 is not settable through the CLI providers this vault uses;
the judge prompt itself asks for one word, and the parse is the authors'.
