# Hook-time symbol hints: a derived projection of one generation (#24, C1)

`scripts/code_hints.py` and the tests that exercise them most.

A hook is a fresh process on every tool call and the validated generation reader costs about two seconds to open from cold (measured 2026-09-11 on a 558-source fixture: open 2 003-2 077 ms, `search_nodes` 501-513 ms), so a `PreToolUse` hint cannot ask the query engine. Instead every index build exports, once, the symbols of the generation it built - qualified name, kind, first location and the resolved in/out degree `search_nodes` ranks by - into one small SQLite file per checkout under `cache/code-hints/`. The hook asks that file one indexed exact-name question.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/code_hints_helpers.py tests/test_a_route_and_a_snippet_say_what_they_left_out.py tests/test_code_hints.py tests/test_the_vault_hint_table_is_made_from_its_code_generation.py -q`.
