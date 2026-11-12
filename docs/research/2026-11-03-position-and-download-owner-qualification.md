# Qualify current position refresh and pinned transfer owners

Date: 2026-11-03. Internal qualification under the approved completion mandate.
No production move, new runtime path, command change or dependency is proposed.

`fresh_positions.py` already owns read-only Python definition/line refresh. Keep
its current path and parser dependency. Include its existing unit tests whole,
without changing assertions; navigation integration tests retain their obligations
in the project inventory. The contract distinguishes one-based Python AST lines
from zero-based LSP positions and does not claim full semantic resolution or an
LSP replacement. Unreadable/invalid/oversized sources retain indexed positions;
file-budget exhaustion remains explicitly marked. Existing limits and their
recorded qualifications are preserved, not newly certified as optimal.

`pinned_download.py` already owns strict response selection, streaming under an
absolute deadline and transient retry classification. Keep its current path.
Move only the existing five retry tests and helper, plus the opener configuration
test and its small stub, into its owner tests with exact AST bodies and decorators.
Partial-file truncation, installer lock/adoption/integrity and real installer
integration assertions stay in their existing test files. Count actual full
bounded-IO/native dependency source and associated owner guides; do not hide
integration work as an unbudgeted shared packet. The downloader validates response
status and URL, not an artifact digest; installers own digest validation.

Alternatives rejected: move already-independent product files solely for naming;
merge unrelated navigation and transfer policies; pull installer executors into a
leaf test helper; replace real integration assertions with mock success. Existing
unit test transfers preserve behavior; provider/consumer integration is separately
rerun, never treated as covered solely by isolation checks.

Sources checked today (independent primary publishers):
- PSF AST location semantics and parse behavior:
  https://docs.python.org/3.14/library/ast.html
- Microsoft LSP position/encoding contract:
  https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/types/position.md
- Tree-sitter basic parsing and syntax-tree positions, considered as an alternative
  parser rather than introduced into this Python-only correction owner:
  https://tree-sitter.github.io/tree-sitter/using-parsers/2-basic-parsing.html
- PSF opener/proxy/redirect behavior and blocking timeouts:
  https://docs.python.org/3.14/library/urllib.request.html
- OWASP redirect validation hazards:
  https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- IETF HTTP response and redirect semantics:
  https://www.rfc-editor.org/rfc/rfc9110.html

These sources support boundary and contract choices, not measured performance or
whole-project completion. Qualification requires fresh graph/source investigation,
exact transfer checks, unchanged production bytes, real related regression,
CCN/shape, unique ownership and full exact-reconstruction packets <=100000 tokens.
