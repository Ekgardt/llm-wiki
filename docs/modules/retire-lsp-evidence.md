# Retire old LSP failure roots under `run/lsp/`, keeping the newest as evidence

`scripts/retire_lsp_evidence.py` and the tests that exercise them most.

A failed language-server start leaves `run/lsp/<owner-nonce>/failure.json`, and the contract left such roots "for the operator", who had no command for them: 80 roots sat on the live vault on 2026-09-23. A root whose owner is not live and whose failure is older than `MAX_AGE_DAYS` is removed once more than `KEEP_NEWEST` such roots exist. Live owners and younger evidence are never touched. Run by the nightly pass. Research: `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
