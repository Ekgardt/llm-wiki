# LSP credential-text redaction boundary

Date: 2026-11-03. Base: daf01519. Designed under the approved decomposition
mandate; implementation/qualification pending in the isolated candidate.

## Responsibility

Move 14 existing functions (_quoted_value_end through _redact_url_userinfo,
plus redact_private_key_blocks) and six corresponding regex/marker/boundary
constants from lsp_security to scripts/llm_wiki/lsp_credentials.py. The owner
uses re and the existing lsp_text._is_control predicate. Preserve every body,
regex, marker and signature. lsp_security keeps four aliases for current callers
and fault-injection sites; the public PEM helper keeps its serialization module.

The three private string stages receive already bounded/normalized text from
redact_lsp_text. The multiline PEM helper is separately called by lsp_process
before line splitting and tail truncation. Neither operation moves type/size
admission, path redaction, containment or final output clipping. The lower owner
must not import repository, filesystem, process or runtime coordination modules.

## Current primary-source research

Three independent publishers checked 2026-11-03:
- IETF RFC 3986 section 3.2.1 warns against displaying password-bearing userinfo;
  RFC 7468 describes textual BEGIN/END boundaries. Keep current complete userinfo
  replacement and whole/incomplete PEM handling rather than splitting lines first.
  https://www.rfc-editor.org/rfc/rfc3986.txt
  https://www.rfc-editor.org/rfc/rfc7468.txt
- OWASP Logging Cheat Sheet names access tokens, authentication passwords and
  encryption keys as data to exclude, separately from log-input sanitization.
  https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- GitHub's supported secret patterns distinguish generic/provider categories and
  supported detector coverage. A finite prefix/regex set is not universal DLP.
  This transfer preserves current coverage and makes no expanded detection claim.
  https://docs.github.com/en/code-security/reference/secret-security/supported-secret-scanning-patterns

Alternatives: using secret_redact here changes its distinct capture-oriented
heuristics and accepted inputs; generic URL parsing alone does not scan multiple
log tokens; moving the whole LSP security owner mixes filesystem access with pure
text. Keep one existing credential transformation policy, no new dependencies,
limits, detection patterns or token-validation service. Python >=3.10 remains.

## Verification plan

Refresh built-in graph, inspect all helper and constant consumers, preserve root
patch points for _redact_assignments/_redact_url_userinfo. Save source and public
pickle baseline. New independent string cases run against original functions,
then only imports change. Structural guards fail before extraction. Retain all
original security/stderr tests and prove every production body equal. Check real
CCN/shape, Python 3.10/3.12, actual lossless complete-packet serialization under
100000 o200k_base tokens, source/guide closure, full regression checkpoints and
platform limits. No live installation is part of this work.

## Implemented qualification

Built-in index at daf01519: generation-18e46ea232b76bca-efa5ba72;
caller graph saved in logs/lsp-credentials-graph.json, with source inspection for
coverage gaps. All 14 function bodies and six constants are AST-identical; every
remaining original root definition is identical. Security tests, stderr-tail
tests and lsp_process are byte-identical. Public PEM pickle bytes are identical.
25 new cases passed against the old owner. Four boundary/identity guards failed
before extraction and passed afterward. Combined checks: 218 passed/24 skipped;
Python 3.10 with packet test: 219/24. Actual CCN and shape plus Ruff passed.
Initial complete packet: 75904 o200k_base tokens. All 56 serialized packets
round-trip every path and byte, largest 97242 before this evidence addition.
Counts include full source/guide/bootstrap closure and exact-duplicate framing;
overlapping packet totals are not unique-project size or task-cycle savings.
Full checkpoint 4abf9ccd completed: 15707 passed, 244 skipped, five warnings,
1784.24 seconds; it predates this extraction. Native Windows/macOS CI remains
unconfirmed (dispatch permission denied), not silently counted as passed.
Global module/ownership/source-closure/structure/complexity/privacy/weight guards:
1379 passed, two skipped, 147.09 seconds. Every packet remains under the fixed
100000-token ceiling; the final artifact rebuild includes this evidence.
Process/stderr consumer regression: 217 passed, 44 platform skips, 165.21 seconds.
