# Per-path index coverage: indexed, fresh, parsed, how many nodes — or why not

`scripts/path_coverage.py` and the tests that exercise them most.

CODE-05, roadmap 2026-08-28: a negative answer is worth exactly as much as the coverage behind it. Parity with codebase-memory-mcp's per-path coverage, including its discipline: best-effort signal, never proof of completeness.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_coverage_reads_only_inside_the_repository.py tests/test_path_coverage.py tests/test_query_surface.py -q`.
