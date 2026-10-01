# Redacted markers keep their closing markup

Researched 2026-09-29 against Python 3.14 and the installed v5.0.0 code.

## Reproduced cause and affected path

A stored daily line already contained `?token=[REDACTED])`. Transport redaction
matched the query value through the closing parenthesis and replaced it again,
removing the parenthesis. A model quoting the original line consequently met two
different refusals: immutable quote validation compared different bytes, and
output DLP treated the harmless formatting change as protected content. A
sanitized observation of the live failed response named the same query-marker
shape. The raw model response and credentials were not retained.

The minimal public reproduction is
`(https://example.invalid/?token=[REDACTED])`. Before the fix the closing
parenthesis disappears and `require_safe_model_output` raises. A closing
backtick and the command-value rule have the same failure. Graph traversal and
source inspection connect `redact_secrets` to capture, transport, output DLP,
publication, session records, structured diagnostics and error descriptions.

## Current independent primary sources

- [CommonMark 0.31.2](https://spec.commonmark.org/0.31.2/#code-spans) defines
  backtick delimiters and bracket/parenthesis link structure. They belong to
  the surrounding representation, not to the redaction marker.
- [Python re](https://docs.python.org/3/library/re.html#re.sub) supports callable
  replacements and `Match.expand`, retaining the existing replacement rules.
- [OWASP logging guidance](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html#data-to-exclude)
  requires excluding credentials from logs. This change preserves that boundary;
  diagnosis records only already-scrubbed context.
- [RFC 3986 section 3.4](https://www.rfc-editor.org/rfc/rfc3986#section-3.4)
  explains query syntax. Punctuation can belong to a genuine query value, so
  globally stopping credential matches at punctuation would be unsafe.

All four sources were fetched on the research date. No new package, service,
configuration or numeric limit is introduced.

## Decision, alternatives and limits

Use the existing common literal-pattern replacement path. Expand the exact
replacement as before. When the matched original already consists of that
replacement followed only by closing brackets/backticks (`)`, `]`, `}`, backtick),
preserve it. The marker is the redactor's existing reserved output vocabulary;
the suffix is closing Markdown/code/JSON structure. Any other additional
credential content is still replaced. Empty suffixes remain unchanged too.

Rejected: disabling output DLP, per-response allowlisting, changing historical
source bytes or evidence hashes, and excluding punctuation from all credential
matches. Those either bypass protection or alter valid source/credential data.
A new Markdown parser or another redaction engine would add an unnecessary
dependency and would not cover command/log consumers of the same shared rules.

The deliberate scope is already-redacted values. A genuine credential continues
to use the existing conservative full match, including ambiguous punctuation.
This does not assert that every model-generated quote is valid: unrelated
missing or ambiguous quotes must still fail immutable evidence validation.

## Regression evidence

Fourteen closing-markup cases fail on the old code and pass after the fix, for
both URL and command values. The empty-marker cases stay unchanged. Extra real
credential text, including after a marker or marker plus parenthesis, remains
redacted and rejected by output DLP. A source-line test passes the line through
transport redaction, immutable quote lookup and output DLP. Existing provider
prefix, entropy, structured, finding-allowlist and truncation regressions remain
required. The stored live source line is unchanged by the new redactor; full
provider replay and installation are recorded separately in the private report.

## Publication replay: marker spelling, 2026-09-29

The next real publication was quarantined even though its model output had passed.
The generated claim ledger contained a URL entity whose placeholder was `[redacted]`.
The publication redactor changed only those eight letters to uppercase; this
byte difference was incorrectly classified as protected content. The immutable
source quotation used the original uppercase marker and was not the changed field.
An unchanged saved transaction after-image now passes the corrected publication
guard; no source, evidence quote, claim ledger or transaction was manually rewritten.

Placeholder comparison preserves spelling and closing markup. Named values accept
only the finite set of markers emitted by the existing rules, case-insensitively;
unknown marker suffixes still count as credential text. Short password options use
the same replacement rule, preserving an exactly quoted known marker. Real secrets,
extra text after a marker, and invented marker names remain blocked.

The rejected alternative was to canonicalize immutable quotations or bypass the
publication guard. Neither is needed: spellings of an already removed value are
not secrets, while evidence must still match exact source bytes. This changes no
DLP policy or allowed source scope. Eighteen regression cases failed on the old
code; negative credential cases remain part of the same gate.
