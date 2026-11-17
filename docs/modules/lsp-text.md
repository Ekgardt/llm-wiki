# LSP text normalization

`llm_wiki.lsp_text` owns the existing Unicode control-character predicate and
terminal text scanner. It uses only unicodedata and preserves every transferred
function/method body. C0/C1, Cf, line/paragraph separators and modal escape/string
handling keep their existing behavior and the interpreter's Unicode database.
Ordinary Unicode and ordinary spacing remain unchanged.

This is normalization, not credential redaction, raw Unicode validation or path
containment. lsp_security validates/types/bounds the raw input before calling it,
then redacts secrets and roots, normalizes again and clips output. All those
checks and their order remain with lsp_security. No new input/output limit is
introduced here. Root aliases for is_control and normalize_log_text preserve
existing path validation and stage-order instrumentation. There is no second body;
private scanner/helper definitions without external consumers leave the root.

Focused pure cases ran against the original implementation before their import
was retargeted. Structural guards failed before extraction. The unchanged complete
security suite remains an integration obligation; focused tests do not replace
credential, Windows short-path or repository-containment qualification. Run
`pytest tests/modules/lsp_text tests/test_lsp_security.py -q`.

The complete packet includes instructions, source, tests, bootstrap, source
research and measurement codec. No file, process, repository, clock or native
platform dependency is permitted. This stage is not a terminal emulator and
makes no new protocol-conformance promise.

## Structural reference retained from the overview

### LSP text normalization

`llm_wiki.lsp_text` owns the existing Unicode control policy and terminal-text
scanner. `lsp_security` retains input bounds, redaction, path containment and output
clipping, calling the same functions through aliases. See
[research](../research/2026-11-03-lsp-text-normalization.md).

Full contracts: [Language registry](lsp-profiles.md), [LSP text](lsp-text.md).
