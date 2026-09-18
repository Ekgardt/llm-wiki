# The installed choice of provider travels whole

Dated 2026-09-18. Finding M-D5 of the third audit (medium; the dropped keys are confirmed
by reading, the effect on a live install was suspected and is reproduced here in a test).

Files: `scripts/integration_hook_config.py`,
`tests/test_the_installed_choice_of_provider_travels_whole.py`.

## What was found

- `PROVIDER_ENV_KEYS` persists exactly two variables into the hooks' environment block and
  the scheduler units: `MEMORY_LLM_PROVIDER` and `MEMORY_CLAUDE_MODEL`. Every other
  variable that decides *how* the chosen provider is called is dropped at install time.
- The variables that shape a provider call, all read by `llm_client`: `MEMORY_LLM_BASE_URL`
  (the HTTP endpoint for openai and ollama), `MEMORY_LLM_MODEL` (their model),
  `MEMORY_CODEX_MODEL` and `MEMORY_CODEX_REASONING` (the codex CLI), and `OLLAMA_NO_CLOUD`
  (the switch that turns the local-only contract on).
- The consequence for the verified local-only mode is the sharp one. An install run as
  `MEMORY_LLM_PROVIDER=ollama OLLAMA_NO_CLOUD=1 MEMORY_LLM_BASE_URL=http://127.0.0.1:11434/v1`
  gives the operator a local-only session, but the nightly and weekly units it writes carry
  only `MEMORY_LLM_PROVIDER=ollama`. There `_wants_local_only` is false, so the loopback
  requirement is not enforced, `local_only_enforced` is not set, and the endpoint falls back
  to the default `http://localhost:11434/v1` with the default model. Unattended runs are
  therefore not in the mode the operator installed.
- `MEMORY_LLM_API_KEY` is the exception: it is a secret, and the install path writes its
  environment into files the scheduler reads.

## Practice on this date

- "Store config in the environment … strict separation of config from code … config varies
  substantially across deploys, code does not" ([The Twelve-Factor App, III. Config](https://12factor.net/config)).
  The corollary the product needs: a deploy's config is the whole set of variables that
  make its behaviour what it is, not a chosen two of them.
- The product's own contract makes this a safety matter rather than a convenience:
  "Verified local-only mode accepts only literal-loopback Ollama and requires verifiable
  Ollama cloud disablement" (`CLAUDE.md`, approved audit-closure contract). A mode that
  only holds in the operator's shell is not the mode the contract describes.
- Secrets are the standing exception. Writing an API key into a user unit file or a hook
  configuration puts it on disk in plain text and into any backup of the vault, which is why
  the installer does not persist it and asks the operator's environment for it at call time.

## The decision

- `PROVIDER_ENV_KEYS` becomes the set of variables that shape a provider call and carry no
  secret: `MEMORY_LLM_PROVIDER`, `MEMORY_CLAUDE_MODEL`, `MEMORY_CODEX_MODEL`,
  `MEMORY_CODEX_REASONING`, `MEMORY_LLM_MODEL`, `MEMORY_LLM_BASE_URL`, `OLLAMA_NO_CLOUD`.
  Each is persisted only when it is actually set in the installing process, exactly as the
  two keys are today, and the `fake` provider still persists nothing.
- `MEMORY_LLM_API_KEY` and `OPENAI_API_KEY` are deliberately not persisted, and the
  research note is where that is written down: an unattended run that needs a paid key must
  get it from the operator's own environment, not from a file the installer wrote.
- No path, runtime layout or contract text changes; the set of keys is a code constant and
  the environment names are unchanged.
