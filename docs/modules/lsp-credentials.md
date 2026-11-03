# LSP credential-text masking

`llm_wiki.lsp_credentials` owns the existing assignment, standalone secret,
URL userinfo and multiline PEM transformations. Fourteen functions and six
constants retain their bodies, signatures, regexes and markers. It imports only
`re` and the Unicode control predicate from the text owner.

Shared canonical contract: [LSP text normalization](lsp-text.md).

The three private stages receive bounded and normalized text from
`lsp_security.redact_lsp_text`. That caller keeps input admission, stage order,
path masking, final normalization and output clipping. The public
`redact_private_key_blocks` runs on whole stderr before line splitting and tail
truncation in `lsp_process`. Moving that call after truncation could expose key
material and is forbidden. Existing integration tests cover that ordering.

Four aliases in lsp_security preserve current consumers and fault-injection
bindings. The public PEM helper retains its module identity for serialization.
There are no duplicate implementations. Removed private scanners and constants
had no external consumers. No filesystem, runtime or process import is allowed.

This is a finite existing detector policy, not universal DLP or credential
validation. Capture-oriented secret_redact has a distinct policy and is not
substituted. No new detection pattern, limit or dependency is introduced.

Run `pytest tests/modules/lsp_credentials tests/test_lsp_security.py
 tests/test_a_stderr_tail_is_redacted_before_it_is_cut.py -q`.
Pure cases first passed against the original owner; structural guards failed
before the transfer. The full packet includes source dependencies and their
contracts, tests, instructions, bootstrap and lossless measurement tooling.

Cross-owner alias/pickle checks live in `tests/test_module_legacy_aliases.py`,
an integration obligation outside this pure owner packet. Their assertions are
preserved; the packet guard now follows imports from test/support files too.

## Structural reference retained from the overview

### LSP credential text

`llm_wiki.lsp_credentials` owns the existing assignment, standalone-token,
URL-userinfo and multiline private-key transformations. Admission, normalization,
path redaction and clipping remain with their current callers; aliases preserve
stage instrumentation. See [research](../research/2026-11-03-lsp-credential-boundary.md).

Full contract: [LSP credential masking](lsp-credentials.md).
