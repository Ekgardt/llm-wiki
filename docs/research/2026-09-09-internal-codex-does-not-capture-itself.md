# Internal classification must not generate another capture

Public source base: [`c8dda9abf6f06e17c3a8143bb1ea16c2fe872588`](https://github.com/Ekgardt/llm-wiki/tree/c8dda9abf6f06e17c3a8143bb1ea16c2fe872588).
The observations and combined regression run were made on a local vault
checkpoint whose scripts, tests, pyproject.toml and uv.lock are byte-identical
to that public base. This source comparison establishes applicability; it does
not relabel the earlier execution as a separate run on public main.

On 2026-09-09, installed capture decisions selected Codex after an unavailable
OpenCode candidate. Timer-only Claude provider settings were not in the Codex
hook environment. Their Codex sessions ran from `/tmp/llm-wiki-provider-*`,
then the normal SessionEnd hook created further flush intents for those
internal classifier sessions. Neutral cwd does not disable user-level hooks.

The [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
documents `features.hooks` as the lifecycle-hook switch. The installed CLI's
read-only `features list` reports hooks=true, and the same command with
`-c features.hooks=false` reports false. No provider was invoked to verify it.
The [hooks reference](https://learn.chatgpt.com/docs/hooks) confirms user/system
hooks can remain active even outside trusted project directories.

Pass that override only to llm_client's internal `codex exec` subprocess.
Keep the existing read-only sandbox, provider/model choice, output path and
ordinary user hooks. This fixes the internal invocation contract without
changing global settings, introducing an environment flag or relying on a
particular provider being selected externally. Mocked subprocess regressions
exercise the actual call path with both implicit and explicit models.

The separate maintenance dispatch defect (capture-intent flush payloads sent
to the legacy prompt-based flush handler) is not fixed by suppressing hooks.
