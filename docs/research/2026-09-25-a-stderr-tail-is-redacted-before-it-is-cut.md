# A stderr tail is redacted before it is cut

Date: 2026-09-25. Audit item C-37 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `lsp_process._failure_stderr_tail` slices the last 1 024 bytes of what the
  failed server wrote (`written[-_STDERR_TAIL_BYTES:]`) and only then redacts,
  one line at a time (`_redacted_lines`).
- The slice can start inside a line. A line `token=abcdef...` cut after `token=`
  reaches the redactor as a bare value; `_CREDENTIAL_ASSIGNMENT` needs the key,
  and `_STANDALONE_SECRET` only knows `bearer`/`basic`, `sk-`, `gh?_`, `xox?-`.
- A PEM private key is several lines: its base64 body lines carry no key word and
  no known prefix, so line-by-line redaction writes them into `failure.json`.
- A generation retains up to `MAX_STDERR_BYTES` (4 MiB) of stderr, so more than
  the tail is available to redact before cutting.

## Source (fetched 2026-09-25)
RFC 7468, "Textual Encodings of PKIX, PKCS, and CMS Structures", section 2,
https://www.rfc-editor.org/rfc/rfc7468.txt:
"Textual encoding begins with a line comprising "-----BEGIN ", a label, and
"-----", and ends with a line comprising "-----END ", a label, and "-----".
Between these lines, or "encapsulation boundaries", are base64-encoded data"
So a key is a block between two boundary lines, and only a block-level pass can
see it; a per-line pass sees base64 with nothing naming it.

## Decision
- Redact a larger window first (the last 64 KiB), and drop its first line when
  the window starts inside the retained text, so no line reaches the redactor
  without its beginning.
- Before the per-line pass, replace every `-----BEGIN ...PRIVATE KEY-----` block
  (to its END line, or to the end of the text when the END is not there) with
  `<redacted private key>`; an END boundary with no BEGIN before it (the BEGIN
  was outside the window) redacts from the start of the text to that line.
- Then cut the redacted text to the record's bound, as before.

## Conclusion / uncertainty
The redactor still recognises only the credential shapes it knows; a bare secret
of an unknown shape written on its own line is not recognisable by any pattern.
That limit is unchanged and stated here, not solved.

## Files
- scripts/lsp_process.py
- scripts/lsp_security.py
- tests/test_a_stderr_tail_is_redacted_before_it_is_cut.py
