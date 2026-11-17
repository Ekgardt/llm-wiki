# Shared fixtures of the retrieval v2 benchmark tests

- `tests/retrieval_v2_helpers.py` — the corpus, schema and runner paths, synthetic lexical
  candidates, the pinned-jieba stand-in and small score/spec helpers; importing it puts
  `benchmark/` on `sys.path`.
- `tests/retrieval_v2_reports_helpers.py` — synthetic candidate reports for the aggregation
  and orchestration tests.

Until 2026-11-10 this module owned the single `tests/test_retrieval_v2_benchmark.py`. That
file was split by concern: each `tests/test_retrieval_v2_<concern>.py` now belongs to the
module of the `benchmark/retrieval_v2_<concern>.py` it tests, and
`tests/test_retrieval_v2_benchmark.py` keeps the SQLite lexical tests (including the
real-jieba test CI selects with `-k jieba`) under `benchmark-retrieval-v2-sqlite-lexical`.
This module keeps only the fixtures those files share.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_retrieval_v2_*.py -q`.
