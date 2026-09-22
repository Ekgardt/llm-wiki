# Quote, then answer; and a refusal calibrated on twins

Dated 2026-09-22. Mechanism 3 of the approved plan
(`docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md`, items 2, 3 and 8), built
from RESEARCH-B (reader), RESEARCH-E §3 (signal detection, Neyman–Pearson) and RESEARCH-F §2.2
(evaluative reporting), all three read on this day. Nothing here reads a benchmark label on
the answer path; every threshold is set on a tune split and frozen before any run.

Files: `scripts/evidence_sufficiency.py` (new), `scripts/query_memory.py`,
`scripts/schemas/grounded-answer-v1.json`, `scripts/refusal_pass.py`,
`scripts/temporal_anchor.py`, `benchmark/longmemeval_data.py`, `benchmark/run_longmemeval.py`,
`benchmark/longmemeval_score.py`, `benchmark/longmemeval_judge.py`,
`benchmark/calibrate_refusal_gates.py` (new, offline only),
`tests/test_the_answer_path_reads_no_benchmark_label.py`,
`tests/test_a_quote_is_checked_by_string_not_by_a_model.py`,
`tests/test_a_refusal_with_the_evidence_in_hand_is_read_again.py`,
`tests/test_a_twin_question_expects_silence.py`.

## What the recorded run says (2026-09-18, 500 questions, read again today)

- 26 refusals on the 470 answerable questions: 24 `insufficient_evidence`, 2
  `unsupported_time_scope`. Every one of them was already refused twice — the refusal look of
  2026-09-08 had searched again and the model refused again on the widened candidates.
- Their reasons are precise. The time-scope ones compute the *event* day from the evidence's
  own words ("'last weekend' in a message captured on Friday 2023-05-26 refers to 05-20/21")
  and then reject a question dated four days later. The relative phrase is loose in the
  dataset and loose in life; the model treats it as exact, against the system prompt's own
  sentence that it is approximate.
- `temporal_anchor.window` resolves "last week", "last Friday" and "N days ago" and nothing
  for "last weekend" (checked today: `window("What game did I finally beat last weekend?",
  2023-05-30)` is `None`). Two of the 26 refusals are "last weekend" questions.
- Three refusals are our own citation gates: `853b0a1d` and `e8a79c70` ("cited span states
  different figures than the claim"), `06db6396` ("cited evidence shares no content with the
  claim"). All three had the gold text in the prompt.
- The recorded rows do not carry the twelve candidates' text or source (the `source` field of
  `lane_matrix` was added on 2026-09-19, after this run), so what the reader saw can only be
  reconstructed from the dataset's labelled evidence turns plus a lexical stand-in for the
  distractors. The offline table below says so on every line.

## What the field says (quoted from the reports, which quote the papers)

- LongMemEval, ICLR 2025 (RESEARCH-B, M4): "even with perfect retrieval, a suboptimal reading
  strategy results in up to a 10-point absolute performance drop compared to the best
  approach for GPT-4o"; "CoN with JSON format outperforms the other three parameter
  combinations by a large margin".
- Du et al., Findings EMNLP 2025: the remedy for the drop that context length alone causes is
  "prompting the model to recite the retrieved evidence before attempting to solve the
  problem", up to +4% for GPT-4o on RULER.
- Slobodkin et al., ACL 2024, "Attribute First, then Generate": select the spans first,
  generate second; attributions "an order of magnitude shorter", fact-checking time nearly
  halved.
- AttributionBench (RESEARCH-B, M3): "even a fine-tuned GPT-3.5 only achieves around 80%
  macro-F1" on "does this citation support the claim". A model-judged attribution check is
  wrong about one time in five; a string check is not.
- Two Axes of LLM Abstention (2026, RESEARCH-B, M1): "answer-confidence tracks whether an
  answer is right but is nearly blind to whether the question is answerable". Sufficiency and
  confidence are two signals.
- RefusalBench, EACL 2026: "False Refusal Rate (FRR): The proportion of answerable instances
  incorrectly refused" and "Missed Refusal Rate (MRR): The proportion of unanswerable
  instances incorrectly answered" — both, always, apart.
- AgentAbstain (2026, RESEARCH-B §3.1, §5): every question comes as a pair, act / abstain, and
  the metric is being right on both sides. Our twins are that pair built from `has_answer`.
- Neyman & Pearson 1933 (RESEARCH-E §3): the likelihood-ratio test "is a uniformly most
  powerful (UMP) test in the set of level α tests". Chow 1970: "The error rate can be directly
  evaluated from the reject function". Geifman & El-Yaniv, NeurIPS 2017: the user fixes the
  risk, the threshold on confidence is chosen to hold it. Signal detection: d′ = Z(hit rate)
  − Z(false-alarm rate), and d′_a = √2 · Z(AUC). RESEARCH-E's rule: "if d′ is below 1 the
  signal is useless at any threshold".
- ENFSI Guideline for Evaluative Reporting (RESEARCH-F §2.2): "Balance – The findings should
  be evaluated given at least one pair of propositions"; "Transparency — derived from a
  demonstrable process in both the case file and the report".

## Decisions

**A. Quote, then answer.** Each claim carries `quotes`: verbatim spans of the cited evidence,
written before the claim's text (the schema lists `quotes` first, so the model writes them
first). The gate is a string check: after whitespace is collapsed and case folded, every
quote must occur inside one of the spans the claim cites. A claim whose quotes all occur has
touched its evidence by construction, so the word-overlap gate does not run on it; the
figure gate still runs, against the quotes and the manifest path, since a figure the claim
states should be in what it quotes unless the claim is derived. A claim without quotes keeps
the old gates unchanged, so a provider that ignores the instruction is verified as before.
The instruction is one sentence; its exact byte cost is in the table below.

**B. Two signals.** `evidence_sufficiency.sufficiency(question, spans, asked_on)` is
deterministic: the question's aspects are its content terms (stemmed), its figures, and its
dates (the window `temporal_anchor` resolves); each is covered or not by the shown spans, a
date aspect within `DATE_SCOPE_DAYS` of the window. The score is the covered share. It is
recorded on every answer as `sufficiency` and it decides one thing: what the refusal look
does. When the model refused and the shown evidence covers the question at or above
`SUFFICIENT_TO_READ_AGAIN`, the remedy is not another search — the evidence is in hand — but
one more reading with the coverage stated as data beside the question (which terms, which
dates, how many days from the window). Below the threshold the look searches, as it has
since 2026-09-08. One regeneration per question, as before; a second look never turns an
answer into silence, as before. Confidence stays the model's; the thresholds are ours.

The date-scope gate is the date aspect: "last weekend" joins `temporal_anchor` as a stretch
(Saturday and Sunday of the week before the anchor's), the same shape as "last week", never
written into an entry as a dated fact. `DATE_SCOPE_DAYS` is the calibrated tolerance.

**C. Twins.** `longmemeval_data.twin_of(question)` copies an answerable question and removes
every session named in `answer_session_ids` from its haystack; the twin's id is the original's
plus `_twin`, its category `twin`, and its right answer is silence. `run_longmemeval.py
--twins` appends one twin per answerable question of the sample. The vault builder needs no
hook: a twin is an ordinary question with fewer sessions. The scorer treats a twin like an
abstention row (its gold is an explanation), and the calibration block reports
`twins`, `refused_when_twin`, `answered_when_twin`, and `pairs_right` — the original
answered right and the twin refused — each with its denominator. LoCoMo rows carry no
`answer_session_ids`, so they get no twin until a mapping from `locomo_evidence` exists.

**The rule, stated before any run.** α = 0.10: on the tune split, the read-again look may
fire on at most one unanswerable question in ten. Tune / decide is LongMemEval question-id
hash parity (`longmemeval_data.split_of`), LoCoMo conversations 1–5 / 6–10. The threshold
that holds α on tune with the most coverage is frozen into the code; the decide half is
reported, never tuned on. Where d′ < 1 the note says the gate cannot be saved by a threshold.

## Offline evidence, measured (`benchmark/calibrate_refusal_gates.py`, 2026-09-22)

The stand-in, stated first: the recorded rows carry no candidate text, so a reading is the
dataset's labelled evidence turns plus the haystack turns sharing the most question terms
(twelve in all); a twin is the same stand-in with the evidence sessions removed. A run with
`--twins` replaces the stand-in with the reader's real window.

**The first measure failed.** Coverage as the union over all twelve spans could hold no
false-fire rate at any threshold: on more than a tenth of the twins the twelve distractors
state every word of the question between them (AUC 0.797, but the score 1.0 is reached by
too many twins for α = 0.10). The shipped measure is the best single span — one span that
states the terms, the figures and a day inside the window together is what an original has
and a twin lacks. Same AUC, a threshold that exists.

LongMemEval, 470 answerable questions, tune 231 / decide 239 by id hash parity, α = 0.10:

| half | mean original | mean twin | AUC | d′ (from AUC) | threshold | coverage of originals | false fire on twins | d′ at threshold |
|---|---|---|---|---|---|---|---|---|
| tune | 0.653 | 0.415 | 0.801 | 1.20 | 9/13 = 0.692 (set here) | 0.398 | 0.065 | 1.26 |
| decide | 0.642 | 0.431 | 0.771 | 1.05 | frozen 0.69 | 0.402 | 0.084 | 1.13 |

The 26 recorded refusals on answerable questions: 13 would be read again (10 of them with the
evidence in the candidates by the run's own `coverage.all_turns`), 13 would still search as
today. Of the 30 rows whose right answer is silence, 22 would still refuse and 8 would be read
again — a second reading, not an answer; what the model then says is for the run.

The date tolerance could not be calibrated: only 7 tune and 13 decide questions resolve to a
window at all (d′ 1.63 on 7 rows, 0.44 on 13). `DATE_SCOPE_DAYS` stays the product's three
days, uncalibrated, and the constant says so.

LoCoMo, 233 answerable questions with evidence, tune conversations 1–5 (121) / decide 6–10
(112): AUC 0.625 / 0.629, **d′ 0.45 / 0.47 — below 1**. The signal does not carry to LoCoMo:
its questions are short and its evidence is one turn of two people's small talk, so the
question's words rarely stand together in one span. At the frozen threshold the look fires on
0.050 / 0.045 of the twins (α holds) and reaches 0.099 / 0.080 of the originals; none of the
24 recorded LoCoMo refusals would be read again. On LoCoMo the gate is safe and inert, and no
threshold saves it — said plainly, as the plan requires.

**Token cost, measured.** The system prompt grows from 5 204 to 5 837 bytes (+633, +12%);
against a mean 4 170 estimated prompt tokens per question on the recorded run that is about
+0.16k a call. The reply grows by the quotes: the recorded answers carry 1.93 claims on
average, so at one 120-character quote per claim the reply grows by about 230 characters
(~58 tokens); the hard cap is four quotes of 400 characters per claim. The actual growth is
for a run to record.

**LoCoMo carried no labels (found by the cover agent, fixed here).** The converted LoCoMo
question names its evidence as `D7:19` under `locomo_evidence` and the stand read only
`answer_session_ids` and `has_answer`, so on the 2026-09-19 run `coverage.sessions_labelled`
was 0 on all 300 rows and no twin could be built. `longmemeval_data.labelled_by_evidence`
writes both labels at question preparation (`run_longmemeval._dataset`); the converter keeps
every turn in order (5 882 turns on the source, none empty, every `dia_id` equal to its
position), and 6 of 2 815 references are compound (`D8:6; D9:17`), which the parser reads.
On the 300 staged questions of the recorded run all 300 now carry evidence sessions (249 one
session, 31 two, 20 three or more) and 233 twins are buildable. The run's own coverage cannot
be re-derived offline: its rows record no candidate text or source, only ranks.

## Sources (as listed in RESEARCH-B §8 and RESEARCH-E, opened 2026-09-22)

- LongMemEval, arXiv 2410.10813 — https://arxiv.org/abs/2410.10813 (ICLR 2025).
- Two Axes of LLM Abstention, arXiv 2607.08456 — https://arxiv.org/abs/2607.08456 (preprint).
- RefusalBench, arXiv 2510.10390 — https://arxiv.org/abs/2510.10390 (EACL 2026).
- AgentAbstain, arXiv 2607.10059 — https://arxiv.org/abs/2607.10059 (preprint).
- Learn then Test, arXiv 2110.01052 — https://arxiv.org/abs/2110.01052 (preprint).
- Sufficient Context, arXiv 2411.06037 — https://arxiv.org/abs/2411.06037 (preprint).
- Neyman & Pearson 1933, Phil. Trans. R. Soc. A 231; Chow 1970, IEEE Trans. IT; Geifman &
  El-Yaniv 2017, NeurIPS — as quoted in RESEARCH-E §3.
- ENFSI Guideline for Evaluative Reporting —
  https://enfsi.eu/wp-content/uploads/2016/09/m1_guideline.pdf.

## What only a run can settle

- Whether the model answers on the second reading, and how often the twins' second reading
  produces an invention: the offline table counts what the gate lets through, not what the
  model then says. The 13 recorded refusals the gate would re-read are the upper bound of what
  it can win on LongMemEval; the decision rule then needs both public benchmarks' decide
  halves and the vault set.
- Whether the product's own retrieval separates originals from twins better or worse than
  the lexical stand-in: the stand-in is adversarial (it maximises term overlap), so the
  measured d′ is a floor for LongMemEval, but LoCoMo's d′ 0.45 says the signal itself, not
  the stand-in, is weak there.
- The output cost of the quotes: the instruction's bytes are exact, the reply's growth is a
  bound (claims × quote cap) until a run records real replies.
- Whether the string-checked quotes drop fewer right answers than the overlap gate did: three
  recorded rows say the gates killed gold-in-prompt answers; the new gate is judged on the
  next run's `refused` line.
