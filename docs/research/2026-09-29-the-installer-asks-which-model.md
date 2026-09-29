# The installer asks which model

Date: 2026-09-29. Status: decided; implemented in the same change.

## The owner's requirement

"нужно сделать чтобы пользователь выбирал при установке модель из тех что у него
доступны по подписке" (2026-09-29). Today the memory pipeline's model is whatever
the provider CLI defaults to, unless `MEMORY_CLAUDE_MODEL` / `MEMORY_CODEX_MODEL` /
`MEMORY_LLM_MODEL` happened to be set in the installing shell (then
`integration_hook_config.provider_environment` persists it into the hooks' env and
the scheduler units). On this machine the choice existed only as a hand edit of the
nightly unit, which the 2026-09-28 update moved to a drop-in.

## How each provider says what is available (read today)

- Claude Code (https://code.claude.com/docs/en/model-config): no command lists
  models. Aliases `default`, `sonnet`, `opus`, `haiku`, `fable` (and `best`,
  `opusplan`, `[1m]` variants) resolve per provider and plan; Fable "may bill to
  usage credits on some plans". `claude --model <alias|full id>` selects one for a
  call. So availability on the user's subscription is only known by trying: one
  short `claude -p --model <x>` call answers it.
- Codex CLI (https://learn.chatgpt.com/docs/developer-commands?surface=cli):
  `--model/-m` or `model` in `~/.codex/config.toml`; `codex debug models` prints the
  model catalog as JSON (`--bundled` for the ones shipped with the CLI).
- OpenCode (https://opencode.ai/docs/cli/): `opencode models [provider]` lists the
  models of the configured providers as `provider/model`.
- Ollama: `ollama list` names the local models; OpenAI API: `GET /v1/models`.

## Decision

- One chooser (`scripts/choose_model.py`), used by both installers before the
  install transaction, for the provider `llm_client` would pick:
  1. list the candidates the provider offers (Codex: `codex debug models`;
     OpenCode: `opencode models`; Ollama: `ollama list`; OpenAI: `/v1/models`).
     Claude has no listing: `claude model list` is an open request since
     November 2025 (https://github.com/anthropics/claude-code/issues/12612), and the
     Models API (https://platform.claude.com/docs/en/api/models/list) takes an API
     key, not the subscription. Reading the subscription's OAuth token out of
     Claude's credential store to call it would be handling someone else's
     credentials and is rejected. So for Claude the list is made by asking: each
     documented alias (`sonnet`, `opus`, `haiku`, `fable`) gets one short
     `claude -p --model <alias>` call, in parallel under one deadline, and only
     the aliases that answered are offered;
  2. on a terminal, show them numbered with the current choice (a persisted value,
     else the provider's default) preselected, and read the answer;
  3. verify the chosen model with one short provider call — the only way to know
     the subscription allows it — and ask again on refusal, naming the provider's
     message;
  4. export the choice as the provider's `MEMORY_*_MODEL` so the existing
     `provider_environment` persists it into hooks and scheduler units.
- Without a terminal (`curl | bash`, CI): `--model <name>` sets it; otherwise the
  previous choice is kept, else the provider default, and the installer says which.
- An update keeps the previous choice without asking; rerunning the installer asks
  again with it preselected.

## Trade-offs

- For Claude the list costs one short request per alias (four) at each install
  that asks; that is the only way to show what the subscription allows, which is
  the requirement. For the other providers the listing is free and the chosen
  model gets one verifying call.
- The Claude list is the documented alias set, not an account query: a new alias
  appears here only when the list is updated. Mitigation: any name may be typed,
  and it is verified the same way.
- No model is chosen for the user; the default stays the provider's own.
