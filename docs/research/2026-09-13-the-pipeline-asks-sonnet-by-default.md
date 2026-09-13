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

**The claude CLI provider defaults to `claude-sonnet-5`.** `MEMORY_CLAUDE_MODEL`
still overrides it, so an operator who wants another reader says so; what changes
is that silence now means Sonnet instead of "whatever the CLI is set to".

- One constant, `DEFAULT_CLAUDE_MODEL`, beside the provider table.
- The model reaches the call through the existing `--model` flag; nothing else in
  the call shape changes.
- A published memory number must still name its reader, because the number moves
  with it: Supermemory's own LongMemEval score falls from 0.95 to 0.846 between
  readers.

Why not the alternatives:

- **Leave it to the environment.** That is what produced 18 failures out of 19 and
  a stand that measured the operator's editor setting.
- **Default to Haiku.** Cheaper, and the pipeline's grounded answers have to hold
  a closed schema and refuse without evidence; that is the work Sonnet is named
  for, and no measurement here says Haiku holds it.
- **Pin a dated snapshot id.** It would freeze the reader against improvements and
  break whenever the alias is retired; the alias is what the CLI documents.

## What must be true after the change

- With no `MEMORY_CLAUDE_MODEL` set, the provider's configuration reports
  `claude-sonnet-5`, and the CLI command carries `--model claude-sonnet-5`.
- With the variable set, the variable wins.
- The grounded-QA path answers on this machine without any environment variable,
  where it refused before.

Files: `scripts/llm_client.py`, `tests/test_llm_client.py`,
`docs/research/2026-09-13-the-pipeline-asks-sonnet-by-default.md`.
