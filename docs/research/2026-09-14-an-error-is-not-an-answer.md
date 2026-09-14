# An error is not an answer, and an answer is not an error

Dated 2026-09-14. Items 2.2, 2.3, 2.5, 2.6 and 2.7 of
`docs/AUDIT-2026-09-14.md`. The research before the fix.

## What was found

Five readers of model output, each wrong in one of two directions.

Throwing away a good reply:

- **Compile plans** — `compile_memory._parse_json_object` reads
  `_extract_json_block`: the text from the first `{` to the last `}`. A sentence
  with braces before the plan (`I checked the {slug} rules.`), a note with braces
  after it, or a first draft followed by a corrected one all raise
  `JSONDecodeError`, recorded as `validation_error` and retried. The nightly logs
  show `draft:claude:validation_error` three times on 2026-09-09 and 2026-09-10;
  the replies were not stored, so the cause of those runs is not proven.
- **Contradiction evaluations** — `contradiction_pipeline._validated_evaluation_output`
  calls `json.loads` on the whole reply: a fenced reply or a sentence before the
  JSON is `malformed_output`, and the claim goes to quarantine unevaluated.
  `knowledge/inbox/claims/` holds 102 candidates, all with the same fixed reason.

Taking a failure for a result:

- **Claude CLI errors** — `llm_client._claude_answer` returns stdout whatever the
  exit status. Probed today: `claude -p --model no-such-model-xyz` exits **1** and
  prints `There's an issue with the selected model …` on **stdout**. That text is
  returned as the model's answer; on 2026-09-13 an `API Error: … safeguards
  flagged` reply reached the grounded-QA parser the same way. The rule came from
  2026-08-28 ("no product behaviour is withdrawn"), before any case of a non-zero
  exit with a real answer was seen; none has been seen since.
- **Lint contradictions** — `lint_memory._contradiction_findings` returns `[]` —
  "no contradictions" — for `None` (no provider answered), for any reply that
  contains `NO_CONTRADICTIONS` anywhere, and for findings bulleted with `*` or `1.`
  instead of `- `.
- **Fact keys** — `fact_keys._key_batch` records every turn of a batch as keyed,
  with the keys found or none. An unreadable reply, no reply, or a reply that
  leaves out a turn marks those turns done forever with no keys. The live store
  holds no turns yet, so nothing has been lost.

## Practice on this date

- A CLI's exit status is its contract. Claude Code's print mode "prints the response
  to stdout and exits with code 0"; its maintainers' own bug report states that "the
  CLI should exit with a non-zero exit code for API errors", and it does — an API or
  configuration error exits 1 (probed above). What a failed process printed is
  diagnostic text, to be named in the error, not parsed as output
  ([Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference),
  [print-mode exit on API errors, issue #19498](https://github.com/anthropics/claude-code/issues/19498)).
- The reply-reading ladder already adopted today — a fence, the whole reply, else
  the last complete value after notes — with every caller validating what it gets
  (`docs/research/2026-09-14-the-document-after-the-notes.md`).
- "Absence of evidence is not evidence of absence": a check that could not run must
  say it did not run, not report a clean result; the lint docstring itself promises
  "absence of a provider is reported, not fatal".

## The decision

1. **Compile and contradiction readers use `reply_json.reply_document`.** Size bounds
   and schema validation stay where they are. `_extract_json_block` is removed.
2. **A non-zero exit is a failure.** `_claude_answer` raises `ProviderExited` for any
   non-zero exit; the excerpt names what the process printed on stdout and stderr,
   bounded and redacted as today. A clean exit with empty stdout is still an empty
   answer. The 2026-08-28 test that kept stdout on a non-zero exit is changed to the
   new contract.
3. **Lint says when it could not check.** `None` → one finding
   `(contradiction check did not run: no provider answered)`. Findings are read from
   bullets `-`, `*`, `•` and numbered `1.`/`1)`. With no findings, the reply must be
   the token `NO_CONTRADICTIONS` (decoration aside) to count as clean; anything else
   → `(contradiction check answered in a form it could not read)`.
4. **A turn is keyed only when the reply named it.** `_key_batch` records a turn when
   the parsed reply carries its index — with keys, or with an empty list meaning "states
   nothing". Turns the reply did not cover stay pending and are asked again; the
   nightly step's time budget bounds the cost.

Why not the alternatives:

- **Keep stdout on non-zero exit and look for "Error" in it.** A substring in an
  error message is the shortcut this codebase has refused before; the exit status is
  the mechanism.
- **Retry fact keys a fixed number of times.** It needs a new attempts table in a
  store that has no turns yet; the nightly budget already bounds a night's spend.

Files: `scripts/compile_memory.py`, `scripts/contradiction_pipeline.py`,
`scripts/llm_client.py`, `scripts/lint_memory.py`, `scripts/fact_keys.py`,
`tests/test_provider_exit_is_not_an_empty_answer.py`,
`tests/test_an_error_is_not_an_answer.py`,
`docs/research/2026-09-14-an-error-is-not-an-answer.md`.
