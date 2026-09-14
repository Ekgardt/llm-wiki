# Every session record is redacted where it is written

Dated 2026-09-14. Items 3.1 and 3.4 of `docs/AUDIT-2026-09-14-2.md`. The research
before the fix.

## What was found

- `session_evidence.write_session_evidence` writes what it is given. Of its three
  callers (code graph: `flush_memory._keep_session_record`,
  `flush_memory._keep_transcript_record`, `backfill_sessions._write_one`), only the
  hook path passes text already redacted: the intent's evidence is redacted when the
  intent is built. The detached flush reads the transcript file itself, and the
  backfill reads past transcripts; neither redacts. The audit reproduced a GitHub
  token and an AWS key reaching `knowledge/raw/sessions/`. The directory is private
  (`knowledge/raw/**` denied), but `CLAUDE.md` promises "a redacted copy", and the
  records are read back by consolidation and sent to a model.
- `_frontmatter` writes `f"{key}: {value}"` and the title `# Session {session}` as
  given. A session id with a line break writes extra frontmatter lines — the audit
  reproduced `type: decision` injected into a record.
- Measured on this machine: `secret_redact.redact_secrets` takes 0.16 s on 630 KB
  of transcript-like text and is idempotent (a second pass returns the same text; a
  GitHub token and an AWS key stay `[REDACTED_GITHUB_TOKEN]`, `[REDACTED_AWS_KEY]`).
  The backfill reads up to 8 MB per transcript; the record keeps 512 KB.

## Practice on this date

- OWASP's Logging Cheat Sheet lists access tokens, keys and passwords among data to be
  removed, masked or hashed before it is recorded, and asks for event data to be
  sanitised against injection
  ([OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)).
  Doing it in the one function every path passes through is this codebase's choice:
  a caller-side rule already failed on two of three callers.
- Log and header injection: a value written into a line-structured format must not
  carry line breaks (CWE-117,
  [Improper Output Neutralization for Logs](https://cwe.mitre.org/data/definitions/117.html)).

## The decision

- The record's document is built from redacted text: the body is cut to the record
  bound plus 64 KB first (so a secret at the final cut was whole when it was
  redacted, and an 8 MB transcript does not cost 2 s), then redacted, then bounded as
  before. Frontmatter values and the title are redacted too. The hook path's second
  pass changes nothing.
- Every frontmatter value and the title are written on one line: control characters
  and Unicode line separators become spaces.

Files: `scripts/session_evidence.py`, `tests/test_every_session_record_is_redacted.py`,
`docs/research/2026-09-14-every-session-record-is-redacted.md`.
