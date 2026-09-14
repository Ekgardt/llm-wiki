# OpenCode only with its password, on its documented API

Dated 2026-09-14. Item 3.3 of `docs/AUDIT-2026-09-14-2.md`, decided on the owner's
delegation. The research before the change.

## What was found

- OpenCode is the first provider auto-detection tries (`llm_client` defaults
  `["opencode", "codex", "claude", "openai", "ollama"]`).
- `_probe_opencode` accepts any process on `127.0.0.1:${OPENCODE_PORT:-4096}` that answers
  `GET /health` with status 200. `_call_opencode` then sends the vault's private prompt
  to it with no authentication: `POST /session`, `POST /session/{id}/prompt` (system text
  as a `noReply` part, then the prompt), `DELETE /session/{id}`. Any local process that
  answers 200 on that port — many development servers answer 200 on every path —
  receives compile drafts, classifications and questions.
- The server's own documentation on this date
  ([OpenCode server](https://opencode.ai/docs/server/)): the server listens **without
  authentication by default**; `OPENCODE_SERVER_PASSWORD` enables HTTP basic auth, with
  the user name `opencode` unless `OPENCODE_SERVER_USERNAME` is set. The health check is
  `GET /global/health` (`{ healthy: true, version }`); a message is
  `POST /session/:id/message` with body fields `parts`, `system`, `model`, `agent`,
  `noReply`, `tools`, answering `{ info, parts }`; `DELETE /session/:id` answers a boolean.
  The generated SDK types show `tools` as a map of tool name to boolean
  ([types.gen.ts](https://github.com/anomalyco/opencode/blob/dev/packages/sdk/js/src/gen/types.gen.ts)).
- So the code also calls paths the documentation no longer lists (`/health`,
  `/session/{id}/prompt`). OpenCode is not installed on this machine; nothing here could
  be checked against a live server. There are no tests of the OpenCode path.
- The code graph: `_probe_opencode` ← the provider probe table; `_call_opencode` ← the
  backend table used by `call_llm_result`.

## Practice on this date

- A client that sends private data to a local service authenticates that service; a
  loopback port proves nothing about who listens on it (OWASP ASVS V9, communications
  and service authentication; the reason OpenCode added a server password).

## The decision

- OpenCode is used only when `OPENCODE_SERVER_PASSWORD` is set. Without it the probe
  says unavailable and auto-detection moves on to the next provider; nothing is sent.
- Every request carries HTTP basic auth (`OPENCODE_SERVER_USERNAME`, default
  `opencode`).
- The documented API is used: the probe reads `GET /global/health` and requires
  `healthy: true`; the prompt goes to `POST /session/{id}/message` with the system text
  in `system`, one request instead of two.
- `tools` is not sent: the map needs the server's tool names, which cannot be listed
  without a server to check; the session is still created and deleted per call.
- A test runs the path against a local fake server that enforces basic auth, so the
  request shapes above are pinned. Whether a real OpenCode answers them as documented
  is not verified here.

Files: `scripts/llm_client.py`, `tests/test_opencode_only_with_its_password.py`,
`tests/test_llm_descriptors.py`, `CLAUDE.md`, `AGENTS.md`, `README.md`, `README.ru.md`,
`README.zh-CN.md`,
`docs/research/2026-09-14-opencode-only-with-its-password.md`.
