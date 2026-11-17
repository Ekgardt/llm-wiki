# Fallback capture checks the original UTF-8 bytes

On 2026-10-07 Windows CI rejected the fallback-capture test even though capture
retained the complete transcript. The fixture writes UTF-8 text using ordinary
platform text I/O. On Windows its physical newline is CRLF. The assertion read
that file through `read_text()`, which normalizes CRLF to LF; its expectation was
therefore different from the correctly retained text.

The assertion now decodes the original bytes explicitly as UTF-8. All checks for
one retained event, its session and its turn identifier remain. Production capture
and the fixture's platform text writer are unchanged. This strengthens the byte
preservation check instead of making capture rewrite the owner's transcript.

Two regression controls invoke that same complete original scenario with a real
CRLF text writer and non-ASCII UTF-8 content. Correctly retained content must pass.
A deliberately rewritten view of retained evidence must fail the original
assertion. Before the fix both controls fail: the valid capture is rejected and
newline rewriting escapes the assertion. The connected capture tests pass after
the fix under CPython 3.10.20. Native Windows qualification remains a CI check.

Alternatives considered: forcing this fixture to LF would hide the supported
Windows case; normalizing both assertion operands would miss changed evidence;
changing production capture would violate physical source preservation. Reading
bytes with explicit UTF-8 keeps the original input as the expectation.

Primary sources checked on 2026-10-07:

- [Python 3.10 I/O](https://docs.python.org/3.10/library/io.html#io.TextIOWrapper)
  specifies universal newline translation and explicit text encoding; binary reads
  perform neither encoding nor newline translation.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html) defines UTF-8 JSON text
  interoperability and escaping of control characters inside strings. An escaped
  CRLF in retained JSON remains part of the source text.
- [Microsoft text and binary I/O](https://learn.microsoft.com/en-us/cpp/c-runtime-library/text-and-binary-mode-file-i-o?view=msvc-170)
  documents the platform's text/binary distinction. The actual CI and Python
  regression, rather than assuming a particular C-library implementation, prove
  this fixture's transformation.
