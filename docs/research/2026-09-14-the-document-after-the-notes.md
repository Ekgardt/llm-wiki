# The document after the notes

Dated 2026-09-14. The memory stand lost 24 of 500 questions as provider errors,
and on 2026-09-13 I told the owner these were broken provider connections to be
retried. That was wrong. This is the research before the fix.

## What the rows say

From `lme500.jsonl` (lexical, 500 questions) and `lme500-hybrid.jsonl` (286
questions answered with vectors and the reranker), reading the stored
`raw_reply` of every row, not the 200-character excerpt in its error:

| run | provider_invalid_json | a complete JSON document in the reply | no JSON at all |
|---|---|---|---|
| lexical 500 | 21 | 17 | 4 |
| hybrid 286 | 13 | 12 | 1 |

- Not one of the 34 is a transport failure. Each reply is the model writing its
  reading as prose — `Working: - E1 (2023-05-23): …` — and then, in 29 of 34,
  the whole answer document as bare JSON after it.
- All 29 documents validate against `scripts/schemas/grounded-answer-v1.json`,
  each reply holds exactly one JSON object, and it is the last thing in the reply.
- `query_memory._parsed_answer` reads `json.loads(_unfenced(raw))`. `_unfenced`
  takes a fenced block wherever it sits — the 2026-09-02 fix for thirteen answers
  lost to prose around a fence — but a bare document after prose has no fence,
  so the whole reply is handed to `json.loads` and refused.
- The 5 replies with no JSON wrote `working:` and `claims:` as prose. The system
  prompt says "Write working first: one line per evidence span …; then the claims.
  Output only JSON matching this closed schema". "Working" is a field of that
  schema, but the sentence reads as an order of writing, not of fields.
- The other two stand errors are not this: `harness_failure` on `4100d0a0` and
  `28dc39ac` was the generation build (fixed in
  `docs/research/2026-09-14-a-link-lives-on-one-line.md`), and
  `verification_or_gate` rows are abstentions that carried claims.

The same reader is used four times: `_parsed_answer`, `fact_keys._loaded`,
`aggregation_pass._parsed_groups` and `aggregation_pass._loaded` all call
`json.loads(_unfenced(raw))`, so each of them throws away a document written
after a sentence.

## Practice on this date

- Robust extraction of JSON from model output is a fixed ladder: parse the reply
  directly first; then strip a Markdown fence; then locate the JSON object inside
  surrounding text — "conversational filler … can cause standard JSON parsing to
  fail"
  ([robust JSON extraction for LLM responses](https://github.com/OpenMind/OM1/issues/1700),
  [llm-output-parser](https://pypi.org/project/llm-output-parser/),
  [reliable JSON from LLM responses](https://dev.to/edgaras/ensuring-reliable-json-from-llm-responses-in-php-3ikb)).
  Python's own `json.JSONDecoder.raw_decode` decodes one value starting at an
  index and reports where it ended, which is exactly "the object inside the text",
  with no hand-written bracket matching.
- The stronger remedy is constrained output: the provider validates the reply
  against the schema. The Claude CLI on this machine (2.1.270) has
  `--json-schema`; probed today with the grounded-answer schema it returned a
  valid document in `structured_output` of `--output-format json`, after
  `$schema`/`$id` were removed (its validator does not resolve the draft 2020-12
  meta-schema). With constrained decoding the schema's property order is the
  order of generation, so a reasoning field must be declared before the answer
  fields
  ([Claude Code structured outputs](https://code.claude.com/docs/en/agent-sdk/structured-outputs),
  [field order and accuracy](https://dev.to/ji_ai/why-json-schema-field-order-breaks-structured-output-accuracy-2985)).
  It is not taken here: it changes the provider transport and the generator
  contract that 31 test and stand generators implement, and it helps only the
  Claude backend while Codex and OpenCode stay prompt-only. It is recorded as
  the next step, not done.

## The decision

1. **One reader for every JSON reply.** `query_memory.reply_document(raw)`:
   the fenced block when there is one (unchanged), the whole text when it parses
   (unchanged), otherwise the last JSON object `raw_decode` finds in the text.
   `_parsed_answer`, `fact_keys._loaded` and both `aggregation_pass` readers use
   it. A reply with no JSON object still raises and still reports its excerpt.
2. **The prompt names the field, not an order of writing.** "Write the working
   field first, inside the JSON document: …; then the claims. Write nothing
   outside the JSON document." The field stays optional and is still removed
   before a reader sees the answer.

Taking the document is not taking the provider's word, exactly as the
2026-09-02 fence fix said: it must still validate against the closed schema, and
every claim must still survive its citation gates. The prose around it is
discarded, never shown.

Why not the alternatives:

- **Retry the call.** The 29 documents are already there; a retry pays a second
  prompt of 5 000–98 000 tokens to get what was thrown away. The 5 prose-only
  replies are a prompt defect, which a retry of the same prompt repeats.
- **The first JSON object instead of the last.** Every reply measured had one,
  at the end; notes come before the document, and a note could quote a fragment
  of JSON while the document never precedes the notes.
- **Repair malformed JSON.** None of the 34 was malformed; there is nothing to
  repair, and repairing guesses at content.

## Expected effect, stated as a bound

Replaying the stored replies through the new reader: 17 lexical and 12 hybrid
answers stop being errors and go to the citation gates. How many of them the
gates keep and the judge accepts is not measured here; that needs a run, and
runs wait for the owner's permission.

Files: `scripts/query_memory.py`, `scripts/fact_keys.py`,
`scripts/aggregation_pass.py`, `tests/test_the_document_after_the_notes.py`,
`tests/test_working_before_the_claims.py`,
`docs/research/2026-09-14-the-document-after-the-notes.md`.
