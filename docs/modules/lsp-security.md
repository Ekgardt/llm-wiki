# Contain LSP navigation evidence to a trusted repository and redact logs

`scripts/lsp_security.py` and the tests that exercise them most.

The language server still runs with the operator's permissions. This module is not a sandbox: it validates requested and returned navigation evidence, while the repository and configured Pyright process remain trusted.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
