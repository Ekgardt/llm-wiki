# An unparsable answer must say what it said

Dated 2026-09-13, after the owner asked for the memory stands and every question
failed with one sentence that named nothing.

## The facts

- `benchmark/run_longmemeval.py --full` wrote 18 error rows of 19, each carrying
  exactly `GroundedQAError: grounded QA provider returned invalid JSON`.
- The same call reproduced on the installed vault in 30 s. Spying on
  `query_memory._provider_response` showed what the provider actually said:

  ```
  API Error: Opus 5 (1M context)'s safeguards flagged this message ...
  Details: `[reasoning_extraction]`  Request ID: req_011Cf1ZhhAwNY97EB8NNpPEx
  ```

  432 characters of a named, actionable upstream failure, thrown away and
  reported as a parse error.
- With `MEMORY_CLAUDE_MODEL=claude-sonnet-5` the same prompt came back as a valid
  `grounded-answer/v1` document, so the product was never broken — the reader was.
- An hour went into finding that, and the message needed to say it.

## Practice on this date

1. **An upstream failure must be reported as an upstream failure, not as
   malformed business JSON.** When a response is truncated or refused, SDKs
   raise "Invalid JSON: EOF while parsing an object", which "makes an incomplete
   upstream response look like malformed business JSON instead of treating the
   upstream status as the primary failure"
   ([openai-python #3263](https://github.com/openai/openai-python/issues/3263)).
2. **Carry a bounded, redacted excerpt.** The pattern is a character cap on the
   message after redacting identifiers, and logging the raw payload before
   parsing with secrets and tokens removed
   ([diagnosing response parse failures](https://github.com/swear01/cpachecker/issues/221),
   [bound the input before the scrubber reads it](https://github.com/chris-dev-at/BetterTrack/issues/1853)).
3. **The first characters usually name the real cause.** "If the first character
   is `<`, you probably got an HTML error page instead of JSON"
   ([JSON parse errors explained](https://offlinetools.org/a/json-formatter/json-parser-error-messages-explained)) —
   here the first characters were `API Error:`.

## The decision

`_parsed_answer` keeps refusing, and its refusal now carries **the first 200
characters of what the provider said, redacted** through the vault's own
`redact_secrets`, plus the length of the whole response. One sentence becomes a
sentence with the evidence in it, which is what every row of a failed stand needs
to be worth reading.

This repository already made the same fix one level down: `ProviderExited` exists
because a crashed CLI, a refused request and an empty answer all arrived as the
same empty string, and it now "carries the two facts the process itself
reported". The parse boundary is the same defect at the next layer up.

Bounds: 200 characters, whitespace collapsed, redacted, and never the whole
answer — an oversized excerpt in an error message is its own failure mode.

## What this does not do

It does not repair the JSON, retry with another model, or change which provider a
stand uses. `MEMORY_CLAUDE_MODEL` already picks the reader, and the choice of
reader for a published memory number is the owner's, not a fallback this code
should make silently.

Files: `scripts/query_memory.py`, `tests/test_grounded_qa_errors.py`,
`docs/research/2026-09-13-an-unparsable-answer-must-say-what-it-said.md`.
