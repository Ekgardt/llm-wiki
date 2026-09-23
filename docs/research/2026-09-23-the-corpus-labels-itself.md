# The corpus labels itself

Dated 2026-09-23. The owner's directive: «я не буду делать никаких ручных разметок,
система должна работать автоматически». `OPEN-034` had been left waiting for a person
to review the labels of the classification corpus. That step is removed, and what it
guarded is replaced by mechanisms the system runs itself.

Files: `benchmark/build_flush_corpus.py`, `benchmark/run_flush_classification.py`,
`benchmark/flush-classification-v3.schema.json` (new),
`benchmark/flush-classification-v2.schema.json` (removed),
`benchmark/review_flush_labels.py` (removed), `tests/test_review_flush_labels.py`
(removed), `tests/test_flush_classification_benchmark.py`, `scripts/flush_memory.py`,
`tests/test_the_classifier_reads_the_conversation.py`, `.gitignore`, `CHANGELOG.md`,
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`,
`docs/research/2026-09-23-the-label-review-comes-home.md` (addendum),
`docs/research/2026-09-23-the-corpus-labels-itself.md`.

## What was found (facts, checked on the live vault on 2026-09-23)

1. **The stand measures a prompt the product does not send.** The installed hooks
   (`Stop`, `SessionEnd`, `PreCompact`) go through `integration_adapter` into a capture
   intent, and the queue worker classifies it with `flush_memory._capture_prompt` plus
   `_CAPTURE_SYSTEM_PROMPT`: three lines that name the tokens `FLUSH_OK`, `FLUSH_MAJOR`,
   `FLUSH_MINOR` and never say what a tier means. The stand
   (`benchmark/run_flush_classification.py --adapter provider`) sends
   `build_classification_prompt` plus `CLASSIFICATION_SYSTEM_PROMPT`, the prompt whose
   docstring calls it "the one prompt that decides a session's tier". That prompt is
   reached only by the `flush_memory.py` command line, which no installed hook invokes.
   The 60 capture decisions on this vault (`run/queue-results/capture-decision-*.json`)
   were made by the short prompt: 25 `ok`, 24 `major`, 11 `minor`.
2. **The corpus carries text the product never reads.** `build_flush_corpus._excerpt`
   stores the last 60 000 characters of the raw host JSONL. Since 2026-09-06 the product
   renders the whole file to conversation first and bounds the rendered text
   (`_readable_evidence`, `_bounded_classifier_evidence`;
   `tests/test_the_classifier_reads_the_conversation.py`). In the live corpus built this
   evening, 39 of 40 case transcripts contain `system-reminder` blocks — injected memory
   and rule text, full of the words "decision" and "lesson" — and the first case opens
   inside a token-accounting record. The judge labelled 37 of 40 `major`; the assistant,
   reading the rendered conversation, 21; kappa 0.155. Whether the judge or the input is
   at fault is not decidable from that run, because they were shown different text.
3. **The only remaining manual step was the human review**, and its owner refuses it.
   A tool whose sole purpose is that step is dead under rule 4.

## Practice on this date

- A single model judge carries intra-model bias; a panel of readers with different
  framings agrees better with people than one judge and costs less
  (Verga et al., "Replacing Judges with Juries", 2024, fetched 2026-09-23). With one
  provider on this machine the readers cannot be different model families, so the
  independence here is of the reading, not of the model — stated as a limit below.
- An extractive reading can be verified: a quote either is in the text or is not. The
  product already holds this line for promotion — the nightly consolidation promotes a
  session only with a verbatim quote (`session-promotion-policy-decision`, 2026-08-25) —
  and for claims: "claims with uncertain evidence or evaluator disagreement enter
  quarantine" (`CLAUDE.md`, Stage 2 contract). The corpus follows the same rule.
- A benchmark must feed the system under test its real input, or it measures a
  different system (the 2026-08-23 note's own sources; the raw-JSONL finding of
  2026-09-06 is the local instance).

## The decisions

1. **One classification prompt.** The capture path sends
   `build_classification_prompt(rendered_bounded_evidence, event)` with
   `CLASSIFICATION_SYSTEM_PROMPT`; `_CAPTURE_SYSTEM_PROMPT` goes. The stand then measures
   what the product sends. The parser is unchanged: the rich prompt's answer is a tier
   token on the first line and a Markdown body, which `_parse_capture_wire_output`
   already accepts. Cost: about 600 more input tokens per classification against the
   evidence's 2 000 to 15 000; the answer is bounded at 1 500 tokens as before.
2. **The corpus carries the rendered, bounded conversation** — exactly what the product
   classifies — and both readings and the stand's adapter read that text.
3. **Two automatic readings replace the human review.** The rubric reading of
   2026-08-23 stays (it never names the product's tiers). A second, extractive reading
   asks for the one passage a reader would still need a month later, quoted verbatim,
   and what kind of thing it is; a quote that is not in the transcript (whitespace
   normalised) invalidates that reading. A case is `confirmed` when both readings name
   the same tier, `contested` otherwise. Contested cases stay in the corpus with both
   readings recorded and are excluded from every metric's denominator; the stand reports
   their count and the builder reports the readings' agreement (Cohen's kappa once at
   least 30 cases, the floor of Bujang & Baharum 2017 already cited in the register).
   Labels are `ai-derived` and say so (`label_provenance: readings`); nothing in the
   product calls them human, provisional, or final.
4. **The human review command, its tests, its verdict sidecar and the `.gitignore`
   line for it are removed.** Schema v2 (with `label_reviewed`, `human_tier`) is
   replaced by v3; the only v2 corpus is the private live file, which is rebuilt. The
   hand-written v1 corpus is unchanged and counts as confirmed.
5. `OPEN-034` closes on the mechanism, not on a person: the number it asked for is
   measured by the stand against automatically confirmed labels, with the contested
   share printed beside it. The register records the measurement of this date.

## Limits (rule 3)

- Both readings run on the same provider and model; they disagree less than two model
  families would. Agreement between them is evidence of a stable label, not of truth.
- A confirmed label can still be wrong in the same direction twice. The stand is a
  regression check of the prompt, as the 2026-08-25 decision already says; it is not a
  claim about the product's accuracy against people.
- Building a corpus costs two provider calls per session; a stand run costs one.

## Sources

- Verga et al., "Replacing Judges with Juries: Evaluating LLM Generations with a Panel
  of Diverse Models", arXiv:2404.18796 — https://arxiv.org/abs/2404.18796 — fetched
  2026-09-23.
- Gu et al., "A Survey on LLM-as-a-Judge", arXiv:2411.15594 —
  https://arxiv.org/abs/2411.15594 — fetched 2026-09-23 (abstract: reliability,
  consistency and bias mitigation are the survey's subject).
- `docs/research/2026-08-23-labelling-real-sessions-for-classification.md` and its
  sources; `knowledge/notes/session-promotion-policy-decision.md` (private);
  `run/queue-results/capture-decision-*.json` and the live corpus on this vault,
  2026-09-23.

## Measured, 2026-09-23 night (the live vault, 40 newest held sessions)

First build with the two readings, 38 cases (2 answers were not JSON and were
skipped and named): rubric 34 `major` / 4 `minor`; quote reading 19 `major` /
9 `minor` / 3 `ok` / 7 void; 21 confirmed, 17 contested; readings' kappa 0.248.
The seven void quotes, asked again: two came back verbatim, one was JSON followed
by prose with braces (first-to-last brace lost it), four stitched two real but
non-adjacent sentences. So the builder now takes the first JSON object, grounds a
quote sentence by sentence (one whole sentence of at least four words present), and
stores the passage beside the tier so a void reading is inspectable.

The same 21 confirmed cases (19 `major`, 2 `minor`, no `ok`), three prompts, one
provider and model:

| prompt | tier accuracy | durable content recall | false promotion |
|---|---|---|---|
| short capture prompt (sent until today) | 0.857 | 0.667 | 0.0 |
| `build_classification_prompt` as it stood | 0.905 | 0.476 | 0.0 |
| the same, plus "keep names exactly as the transcript spells them" | 0.952 | 0.714 | 0.0 |

The rich prompt names tiers better and, told to be terse, dropped one of the three
marker terms in nine of eleven misses; one added sentence recovers most of that. The
product now sends the third prompt. The recall gate (0.8, set on 2026-08-19 with no
real data) still fails at 0.714: the remaining misses each lose one term such as a
run number or a short Russian phrase. That is the next thing to work on the prompt,
and the stand now measures it on real sessions with no one labelling by hand.

Second build with the final builder: 39 cases (1 skipped and named); rubric 36
`major` / 3 `minor`; quote reading 28 `major` / 8 `minor` / 3 `ok`, none void; 29
confirmed (28 `major`, 1 `minor`), 10 contested; readings' kappa 0.202. The final
prompt on the 29 confirmed cases: tier accuracy 1.0, durable content recall 0.655,
false promotion not measurable (no confirmed `ok` case: every held session of this
day's subagents carried content). The recall gate fails; each miss loses one term.
The rubric still leans `major` on rendered text (36 of 39); the quote reading is the
brake on it, and the contested ten are where the brake bit.
