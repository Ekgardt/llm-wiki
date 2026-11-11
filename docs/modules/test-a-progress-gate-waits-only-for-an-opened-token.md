# `tests/test_a_progress_gate_waits_only_for_an_opened_token.py`

A progress gate waits the whole deadline only for a server that opened a token.

Split from the transport tests of finding K-B17 of the 2026-09-17 audit.
Research: docs/research/2026-09-18-sess-a-lapsed-deadline-is-not-a-torn-stream.md

Its tests are `tests/test_a_progress_gate_waits_only_for_an_opened_token.py`.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_a_progress_gate_waits_only_for_an_opened_token.py -q`.
