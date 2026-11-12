# A maintenance packet includes the test loader

2026-11-02. Approved completion mandate; measurement correction, no product change.

## Reproduced cause

All 16 context manifests included their own tests but omitted the parent pytest
conftest and its declared plugin. tests/conftest.py loads
`tests.code_kernel_helpers` for the whole suite. These files contain 7511 tokens
under o200k_base. That was a shared undercount, not evidence of smaller modules.
Evidence: logs/module-pytest-bootstrap-underestimate.json. Before implementation,
a regression will fail on these existing manifests rather than merely test a
new counter against itself.

## Correction

A shared test-only `tests/module_context.py` replaces duplicated counters. It
reads the manifest, expands owned directories without dropping new files, checks
that the manifest, counting helper, applicable conftest files and declared local
pytest plugins are explicitly present, and tokenizes the actual serialized bundle.
File names and byte-length headers are part of the bundle. Missing files and
unreadable text fail; no truncation, ignored ownership or silent auto-completion.
The owner limit remains exactly 100000 and the encoding o200k_base.

Plugin declarations are read statically, never executed by the counter. Unsupported
dynamic declarations are an explicit failure; supporting one requires teaching
the reader that contract rather than omitting the loader. Paths must remain inside
the checkout. Plugin registration cycles are traversed once. Third-party installed
pytest plugins remain environment dependencies declared by pyproject/lockfile,
not source files copied into each module. Actual product dependencies are reviewed
separately; this correction is not a claim of complete global ownership coverage.

Regression: existing manifest omission; new ancestor conftest; local plugin chain;
missing file; escaped path; recursive registrations; complete content including
Unicode and text resembling delimiters. No suppression of pytest's isolation or
knowledge-write guards. Tests keep the original module-specific budget test names
where useful while invoking the common measurement.

## Research and alternatives

Checked 2026-11-02:
- https://docs.pytest.org/en/stable/how-to/writing_plugins.html — conftest and
  pytest_plugins are implicit loaders, not optional fixture text.
- https://github.com/openai/tiktoken — count with the selected tokenizer rather
  than infer tokens from byte size.
- https://docs.python.org/3/library/pathlib.html — explicit path enumeration and
  containment. Implementation uses APIs available on Python 3.10.

Keeping 16 hand-maintained counting implementations repeats the same omission.
Silently adding missing files would hide an incomplete declaration. Disabling
conftest to make tests look independent would remove existing safeguards. Chosen:
one small explicit measurement helper and regressions, no package dependency.

## Results

All 16 existing manifests failed the omission regression before correction.
The revised counter includes both loader files, its own implementation and file
framing. Two new test directories also needed explicit __init__.py: individual
runs passed, but joint collection previously produced import-file mismatches.
Those package markers fix the cause without changing pytest's import mode.

The first broad run found three test import guards that did not admit the new
measurement helper (685 passed, 2 skipped). They now admit only the exact helper
for tests, while preserving the production prohibition; a separate guard verifies
the helper itself imports only stdlib/tiktoken. Regression cases prove test-only
permission does not grant product code a new dependency. Final combined run:
744 passed, 2 skipped, including global CCN/shape, structure and CI policy.
Weights completeness: 8 passed. Ruff/diff checks clean. Final serialized counts
are recorded in logs/module-context-complete-budgets.json; maximum at qualification
was 99281 tokens for queue-child-runtime. No context was truncated to pass.
