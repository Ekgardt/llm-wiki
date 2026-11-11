# Vault and state roots read at call time

`scripts/llm_wiki/runtime_roots.py` and the tests that exercise it most.

Resolves `LLM_WIKI_ROOT` and `LLM_WIKI_STATE_ROOT` on every call and derives the
`knowledge/`, `run/` and `logs/` paths from them. No module keeps an import-time copy
of a root; `tests/test_roots_are_read_at_call_time.py` fails on one that does. The
decision and its sources are in `docs/research/2026-11-10-roots-are-read-at-call-time.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_roots_are_read_at_call_time.py -q`.
