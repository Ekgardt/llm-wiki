# A tool line is redacted before it is cut

Date: 2026-09-26. Audit 2026-09-26 C-6.

## Facts

- `session_evidence._tool_line` cut a tool's target to `MAX_TOOL_LINE_CHARS` (200)
  and the record was redacted afterwards. A secret straddling the cut lost its
  shape: `redact_secrets` turns a whole `ghp_…` token into
  `[REDACTED_GITHUB_TOKEN]` but leaves `ghp_A1b2C3d4E5f` as it is (checked
  2026-09-26), so the token's first characters reached the session record and,
  through `backfill_sessions`, old records too.
- The same file already cuts bodies to the bound plus `REDACTION_SLACK_CHARS`
  first, "so a secret at the final cut was whole when it was redacted".
- OWASP's Logging Cheat Sheet
  (https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html, fetched
  2026-09-26): "The following should usually not be recorded directly in the logs,
  but instead should be removed, masked, sanitized, hashed, or encrypted", listing
  "Access tokens", "Authentication passwords" and "Encryption keys and other primary
  secrets".

## Decision

- The tool target is redacted whole, then cut.

## Files

- `scripts/session_evidence.py`
- `tests/test_a_tool_line_is_redacted_before_it_is_cut.py`
- `CHANGELOG.md`
