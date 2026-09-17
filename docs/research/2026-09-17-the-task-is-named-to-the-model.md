# The task is named to the model

Dated 2026-09-17. The research before the fix to the Claude CLI provider call.

## What was found

- In the full measurement of 2026-09-17, 160 of 500 judge verdicts were unreadable and 99
  replies said things like "I'm ready to help. What would you like me to do?". The same kind
  of reply reached the answer step.
- The prompt is not lost. The event stream of eight identical calls (`--output-format
  stream-json --verbose`) shows the same input size for good and bad replies (1006–1009
  tokens), and before the model's turn a `SessionStart` hook event whose output is a
  machine-policy banner. In a bad run the model answered the banner: "this conversation
  doesn't contain any actual question or task … just system configuration details about
  hooks, environment".
- The call already passes `--setting-sources ""`, which drops user and project settings. A
  hook declared in the machine's managed settings still runs: "Managed settings … Nothing you
  set overrides them: a key you pass with `--settings` doesn't override the same managed key"
  ([Claude Code settings, precedence](https://code.claude.com/docs/en/settings)). `--bare`
  skips hooks but reads neither OAuth nor the keychain, so it would need a paid API key — the
  product's zero-cost contract forbids that. Whether to silence its own hook is the machine
  owner's decision, not the product's; any installation with a managed `SessionStart` hook
  has the same exposure.
- Code graph: `_call_claude` ← `call_llm` (every provider call of the product and the
  benchmark stands) → `_claude_command`, `_claude_stdin`.

## Practice on this date

- "XML tags help Claude parse complex prompts unambiguously, especially when your prompt
  mixes instructions, context, examples, and variable inputs. Wrapping each type of content
  in its own tag … reduces misinterpretation."
  ([Prompting best practices, Structure prompts with XML tags](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)).

## Measurement

Three judge prompts that had failed in the run, eight calls each, `claude` 2.1.274,
`claude-sonnet-5`: unchanged call 20 of 24 readable verdicts (one prompt 4 of 8); with the
task wrapped in `<task>` and one sentence in the system prompt naming the frame, 24 of 24.
The sample is small; it shows the direction, not a rate.

## The decision

- The Claude provider wraps the prompt in `<task>…</task>` and adds to the system prompt one
  paragraph: the host may put automated session notices before the message, they are not
  addressed to the model, the whole task is inside the tags. With no system prompt the
  paragraph goes through `--append-system-prompt`, keeping the default persona.
- Cost: about 50 input tokens a call.
- The banner itself costs about 300 input tokens in every product call on this machine; that
  is reported to the owner, the product does not touch the hook.

- The stand's judge used to take a reply that is neither "yes" nor "no" as no verdict and
  silently fell back to the deterministic score, and the report did not say how often. It now
  asks once more (two attempts in all), and the report carries `judge_unreadable`, so a
  contaminated run is visible in its own report. This is the project's own decision; no
  outside source is claimed for it.

Files: `scripts/llm_client.py`, `benchmark/longmemeval_judge.py`,
`tests/test_an_unreadable_verdict_is_asked_again.py`, `tests/test_the_task_is_named_to_the_model.py`,
`docs/research/2026-09-17-the-task-is-named-to-the-model.md`.
