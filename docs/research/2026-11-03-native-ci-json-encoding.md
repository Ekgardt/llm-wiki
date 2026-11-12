# JSON reads must not inherit the host code page

Date: 2026-11-03. Observed bug, not a platform qualification claim.

Windows Python 3.12 job 125828626162 in Actions run 41609824640 failed collecting
model_records/test_contract.py: the synthetic UTF-8 JSON fixture was decoded by
cp1252. The companion public-class compatibility test has the same defect.
A local fresh interpreter with LC_ALL=C, PYTHONCOERCECLOCALE=0, PYTHONUTF8=0 and
-X utf8=0 reproduced collection failure (ASCII default) before any edit. This
changes only the test process environment; no check is bypassed.

Inspecting json.loads(Path.read_text()) across scripts/tests found the same
implicit-code-page pattern in related golden fixtures, consumer compatibility
checks, temporary JSON outputs, and one compiler JSON-schema read. Add explicit
UTF-8 at those reads, preserving original fixture bytes and all assertions.
A repository integration regression checks this direct syntax and exercises
positive/negative examples. It is an integration test with repository input,
not silently part of a leaf packet. It does not claim arbitrary IO dataflow analysis.

The smaller alternatives (changing only the failing fixture, forcing UTF-8 globally
in CI, or escaping all fixture Unicode) leave the cause or hide the platform
mismatch. No production path, JSON value, public command, dependency or limit
changes. Relevant source navigation is in logs/native-ci-correction-graph.json,
indexed at db92e11a; AST source inspection supplements partial call-graph coverage.

Primary sources checked 2026-11-03:
- https://docs.python.org/3/library/pathlib.html#pathlib.Path.read_text — text-read
  encoding is an explicit argument; the implementation delegates to text IO.
- https://learn.microsoft.com/en-us/windows/win32/intl/code-pages — code pages differ;
  use Unicode for portable text rather than assuming a host ANSI code page.
- https://docs.pytest.org/en/stable/how-to/monkeypatch.html — test-scoped environment
  changes are restored; a fresh controlled interpreter can reproduce host defaults.

Evidence: https://github.com/Ekgardt/llm-wiki/actions/runs/41609824640/job/125828626162
and logs/fixture-encoding-reproduction.txt. Other CI findings (installer model
selection, Python ABI wheels, dependency advisories, historical synthetic scanner
matches) are separate work and are not resolved by this correction.

Validation: 52 explicit reads across 42 files; AST proof permits only the encoding
argument change. The new guard failed before correction; the original Unicode
fixtures now pass under an ASCII-default subprocess (59 tests). The directly
affected Python 3.12 set passed 1,034 tests / 4 skips. Python 3.10 initially passed
1,030 / 4 skips and failed four selection tests because its isolated environment
lacked the already-locked tomlkit 0.15.2. Installing that exact hash-pinned wheel
and rerunning the complete 13-test selection file passed; no product dependency
or test was removed. Actual complexity of all 38 changed functions passed.
