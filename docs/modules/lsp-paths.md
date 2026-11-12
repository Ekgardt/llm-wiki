# Managed LSP path derivation

`scripts/lsp_paths.py` derives the existing pinned Pyright and per-owner LSP
paths from a Path state root. It creates no directory, starts no process and
owns no lease. The version/root constants and 32-lowercase-hex nonce validation
remain exact. Paths use the platform's existing pathlib resolution semantics.
Callers remain the installer, profiles and Pyright session owner; public imports
and runtime roots do not change.

The packet owns the full focused tests, source and boundary checks. It includes
all instructions, pytest bootstrap, guide, research and serialization code.
Tests verify constants, rejected inputs and lack of directory creation. Run
`pytest tests/modules/lsp_paths tests/test_lsp_paths.py -q`. Native Windows/macOS
qualification remains separate from the Linux checks.
