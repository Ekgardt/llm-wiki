# A failure while building the answer is still a safe answer

Date: 2026-09-25. Audit item B-20 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `_tool_call_data` wraps the tool itself: any exception becomes
  `_tool_call_failure`, whose message goes through `_safe_exception_text`
  (paths and secrets removed).
- What follows it in `_execute_tool_call` — quality, components, the envelope
  (`_tool_call_envelope`) and its JSON rendering — has no such boundary. An
  exception there (a malformed trace, a value `json.dumps` cannot encode, an
  `OSError` reading a manifest) leaves the handler, and the MCP SDK answers with
  the exception's own text, which can carry a local path.

## Source

- OWASP Error Handling Cheat Sheet,
  https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html
  (fetched 2026-09-25): "When an unexpected error occurs then a generic response
  is returned by the application but the error details are logged server side
  for investigation, and not returned to the user."

## Decision

- Building and rendering the envelope sit behind the same boundary as the tool:
  a non-timeout exception there is answered with the envelope of
  `_tool_call_failure` for that tool, rendered; a timeout still takes the
  timeout path.

## Files

- `scripts/mcp_server.py`
- `tests/test_an_envelope_failure_is_a_safe_answer.py`
- `CHANGELOG.md`

## Follow-up the same day (clean run of b4f3015a)

- Fact: the repository's privacy guard (`tests/test_nothing_private_reaches_the_public_repository.py`)
  failed on this note's test, which used a home-directory-shaped path as its example of a private
  string. The example is now `/srv/private-vault/...`; what it proves is unchanged.
- File: `tests/test_an_envelope_failure_is_a_safe_answer.py`.
