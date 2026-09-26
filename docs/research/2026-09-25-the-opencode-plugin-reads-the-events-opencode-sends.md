# The OpenCode plugin reads the events OpenCode sends

Date: 2026-09-25. Audit items B-6 and C-6 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked against the OpenCode source; no OpenCode runs on this machine)

- OpenCode's generated SDK types (`packages/sdk/js/src/gen/types.gen.ts`,
  repository `anomalyco/opencode`, fetched 2026-09-25 through the GitHub API):
  `session.created` carries `properties: { info: Session }` with `Session.id`;
  `session.idle` carries `properties: { sessionID }`.
- OpenCode's plugin hook types (`packages/plugin/src/index.ts`, same fetch):
  `"tool.execute.after"(input: { tool, sessionID, callID, args }, output)`.
- `scripts/llm-wiki-memory-opencode.js` looks for the session id under
  `sessionInfo.id`, `sessionID` or `sessionId`, so on `session.created` it finds
  none: the session-start context returned by the adapter is never remembered
  and never reaches the system prompt (B-6, first part).
- `integration_adapter._session` reads `sessionInfo` too, and `_tool_payload`
  reads an OpenCode tool's arguments from `input`, where OpenCode sends `args`:
  every OpenCode tool line has an empty target (B-6, second part).
- The plugin drops every event whose directory is inside the vault (`isVault`).
  The prompt and tool hooks stopped doing that on 2026-09-24 and 2026-09-25; the
  memory's own processes are filtered by the adapter's reentry marker (C-6).
- B-6, third part, is not fixed here: `session.idle` fires after each turn and
  each forwards a `session_end` capture, one classification call per turn.
  Coalescing or delaying it trades against losing the last window when OpenCode
  quits, and needs a measurement on a live OpenCode; it stays open. A related
  open question found here: with the OpenCode provider enabled, sessions the
  memory itself opens on the OpenCode server may reach this plugin; the reentry
  marker does not cross into that server.

## Decision

- The plugin and the adapter read the session id from `info.id` first
  (keeping `sessionInfo.id`, `sessionID`, `sessionId`), and the adapter reads an
  OpenCode tool's target from `args` (keeping `input`).
- The plugin no longer drops events from inside the vault.
- An idle whose transcript is identical to the last one forwarded for that
  session is not forwarded again.

## Files

- `scripts/llm-wiki-memory-opencode.js`
- `scripts/integration_adapter.py`
- `tests/test_the_opencode_plugin_reads_the_events_opencode_sends.py`
- `CHANGELOG.md`
