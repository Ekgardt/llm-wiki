# The pipeline asks Sonnet by default

Dated 2026-09-13, at the owner's instruction: the memory pipeline's reader should
be Sonnet, not whatever model the operator's CLI happens to be set to.

## The facts

- `_cli_configuration("claude", "MEMORY_CLAUDE_MODEL")` returns
  `os.environ.get(model_variable) or None`, and a `None` model means the call is
  made with **no `--model` flag at all** — so the CLI answers with whatever the
  operator's session is configured to use.
- On this machine that is Opus 5 (1M context), and it **refuses** the grounded-QA
  prompt: `API Error: ... safeguards flagged this message ... Details:
  [reasoning_extraction]`. Measured today: 18 of 19 questions of a LongMemEval
  run failed that way.
- With `MEMORY_CLAUDE_MODEL=claude-sonnet-5` the same prompts answer, and a probe
  of 8 questions returned 6 answered, 1 insufficient_evidence, 0 errors.
- So the reader of every compile, classification and grounded answer was decided
  by an unrelated setting, and the pipeline's own behaviour changed when the
  operator changed their editor's model.

## Practice on this date

1. **A production stack is tiered, and background work does not run on the
   frontier model.** "The 2026 production stack is two-tier: frontier models for
   hard reasoning, small task-tuned models for routing, classification,
   rephrasing, and intent detection", and "most teams overpay by defaulting to a
   flagship for jobs a $0.28 model finishes just as well"
   ([AI model tier strategy](https://www.institutepm.com/knowledge-hub/ai-model-tier-strategy)).
2. **Sonnet is named as the mid-tier default for routine work**: "Claude Sonnet 5
   balances cost and performance for routine applications"
   ([best AI models 2026](https://blog.buildfastwithai.com/best-ai-models-2026-full-ranked-analysis-and-benchmarks)).
3. Rule 4's own words — a system that uses an LLM must spend tokens sparingly —
   point the same way for classification, compilation and QA, which is all this
   provider is asked to do.

## The decision

**The reader stays the operator's choice, and this machine is configured to
Sonnet.** The owner's words: the user decides which model to use; put Sonnet 5 on
the local machine. So no default goes into `scripts/llm_client.py` — with
`MEMORY_CLAUDE_MODEL` unset the call still carries no `--model` flag, and the
docstring now says why that is a configuration question rather than a code one.

Configured on this machine, outside the repository:

- `~/.claude/settings.json`, `env.MEMORY_CLAUDE_MODEL = claude-sonnet-5` — every
  agent session and the MCP server it launches;
- `~/.config/systemd/user/llm-wiki-nightly.service` and `llm-wiki-weekly.service`,
  one `Environment="MEMORY_CLAUDE_MODEL=claude-sonnet-5"` line each — the nightly
  and weekly passes, which is where compilation and classification actually run.

`systemctl --user daemon-reload` has no bus in this container; the owner's own
session needs to run it once for the units to be re-read.

Why not a default in the code:

- It would decide for every install what only the operator can know, and it would
  hide the choice in a file nobody reads when a number moves.
- A published memory number must name its reader anyway — Supermemory's own
  LongMemEval score falls from 0.95 to 0.846 between readers — so the reader
  belongs in the configuration that produced the number, visible beside it.

## What must be true after the change

- With no `MEMORY_CLAUDE_MODEL` set, the provider reports no model and the CLI
  command carries no `--model` flag: the code invents nothing.
- With the variable set — as it now is on this machine — the CLI carries that
  model, and the grounded-QA path answers where it refused before (59.1 s,
  status=answered).

Files: `scripts/llm_client.py`, `tests/test_llm_client.py`,
`docs/research/2026-09-13-the-pipeline-asks-sonnet-by-default.md`.
