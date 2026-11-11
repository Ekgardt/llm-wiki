# `tests/test_lsp_log_redaction.py`

Its tests prove language-server text reaches logs with repository roots, home paths, control sequences and secrets redacted, in bounded time.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_lsp_log_redaction.py -q`.
