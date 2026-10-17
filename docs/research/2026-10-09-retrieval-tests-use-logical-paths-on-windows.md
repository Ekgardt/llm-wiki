# Retrieval tests use logical paths on Windows

Date: 2026-10-09.

The complete Windows CI run for 6049a215 exposed an error in the new Markdown fallback regression: search correctly returned normalized logical paths with `/`, while the test built expected values with `str(Path.relative_to(...))`, which uses `\` on Windows. The expected set now uses `as_posix()`. All candidates, original set equality, subset, relevance companion and size assertions remain intact. The production search is unchanged.

The defect was confirmed by actual Windows CI on Python 3.10–3.14. The six original tests passed locally on Python 3.10 and 3.14 after the correction. Actual Windows rerun is still required. Other Windows hook and access-export deadline failures in that run remain separate findings and are not called fixed by this test correction.

Research checked 2026-10-09: [Python pathlib](https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.as_posix) distinguishes native string paths from portable POSIX serialization; [pytest temporary-path documentation](https://docs.pytest.org/en/stable/how-to/tmp_path.html) specifies concrete platform `Path` fixtures; [Microsoft path conventions](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file) describe Windows native separators. Comparing logical-path strings in a common representation preserves the contract. Loosening equality or changing search's portable serialization would hide the test defect.

Source: actual Windows jobs in run 37946903350 and `tests/test_admission_prior_does_not_also_order_fusion_signals.py`.
