# Repair asks about the provider the operator chose

Dated 2026-09-17. Finding M-A9 of the third audit (low/medium, confirmed by reading and by
a test). The research before the fix.

Files: `scripts/llm_client.py`, `scripts/doctor.py`, `scripts/compile_memory.py`,
`tests/test_repair_asks_about_the_provider_the_operator_chose.py`.

## What was found

- `doctor._ready_capabilities` decides whether `doctor --repair` flips `blocked` model
  tasks back to `ready`. It calls `provider_candidates()` with no forced provider, so it
  probes the whole automatic order, while every caller that actually makes a call reads
  `MEMORY_LLM_PROVIDER` and is strict about it.
- With `MEMORY_LLM_PROVIDER=ollama`, Ollama down and any `claude` binary on `PATH`, repair
  reports the capability ready and unblocks every task; the worker then runs each against
  the forced, dead provider, fails it and spends one of its attempts.
- The environment variable is read and normalised in three places by three copies of the
  same line (`llm_client.call_llm_result`, `compile_memory`, and missing in `doctor`).

## Practice on this date

- "The twelve-factor app stores config in environment variables", and "env vars are
  granular controls, each fully orthogonal to other env vars"
  ([The Twelve-Factor App, III. Config](https://12factor.net/config)). One control has one
  meaning: a readiness check that ignores the control the calls obey is answering about a
  different system.

## The decision

- `llm_client.forced_provider()` is the one reader of `MEMORY_LLM_PROVIDER`;
  `call_llm_result`, `compile_memory` and `doctor._ready_capabilities` use it.
- Repair unblocks model tasks only when the provider the calls will use answers its probe.
- Known limit, unchanged: the `codex` and `claude` probes test that the binary exists, not
  that it is logged in. No path, environment variable or contract changes.
