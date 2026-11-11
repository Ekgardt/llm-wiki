# Source-reading boundary tests require declared context

Date: 2026-11-03. Status: correction in progress; no project completion claim.

A compile-answer-records boundary test reads all of memory_queue.py, absent from
its manifest. The import closure cannot discover Path.read_text dependencies.
The same pattern occurs in queue, filesystem, Windows, formats and LSP checks.
Reproduction: logs/context-source-read-reproduction.json; current built-in caller
graph: logs/context-source-read-graph.json at 67aebe6c. Graph coverage is partial;
all boundary sources were searched and consumer test bodies inspected directly.

Under the approved completion mandate, preserve consumer-source assertions as
integration tests grouped by their consuming subsystem, outside pure packets.
They remain required in regression and remain unassigned integration work until
separately qualified. Moving them does not certify a complete project.
Pure boundary source reads must use a shared manifest-checked reader, resolving
paths and refusing undeclared inputs. A static guard rejects direct read_text,
read_bytes and open calls in these boundary files, requiring this checked path.
Tests exercise missing inputs and symlink escapes as well as declared inputs.
This is not a claim to trace arbitrary Python IO or subprocess program strings;
existing subprocess fixture reads and isolated imports need explicit review.

Alternatives: adding entire consumers and their import closures exceeds the limit
for the existing queue packets; deleting assertions loses protection; a general
symbolic interpreter is disproportionate and still incomplete. Full extracted
packet execution remains useful additional evidence but is not replaced by the
static guard. No production runtime behavior or dependency changes are intended.

Primary sources checked 2026-11-03:
- https://bazel.build/basics/hermeticity — explicit inputs and isolated execution
  help reveal undeclared dependencies. No Bazel adoption is proposed.
- https://docs.pytest.org/en/stable/how-to/monkeypatch.html — scoped replacement
  restores state; useful for reader negative tests, not a filesystem sandbox.
- https://docs.python.org/3.10/library/pathlib.html — resolve follows symlinks;
  lexical containment alone does not prove resolved containment. Python 3.10 is
  upstream unsupported; project compatibility remains unchanged.

Validation must retain all moved assertions, exercise both Python versions,
measure actual CCN, run integration checks plus all packet gates, and serialize
and round-trip each complete declared packet with o200k_base measurement.

Observed correction evidence: the new regression initially failed 53 checks.
The manifest reader then exposed two additional whole-script scans in queue
contracts and reliability defaults. Those scans and their guard regressions were
also preserved in integration tests. All 229 pre-existing boundary functions
were compared by AST (normalizing only global names, checked read calls and the
explicit reader argument); 61 functions moved, with all 140 assertions retained.
The shared stdlib-boundary helper now requires the checked reader too. Its source
is included in the direct-read regression. Remaining module functional-test reads
were reviewed: declared fixture files or temporary outputs; subprocess receipt
fixtures are declared as whole files. Dynamic isolated imports remain separately
guarded, not falsely claimed as an exhaustive IO sandbox.

The first pass had two real context failures (whole-script scans), then a missing
pytest import in the integration relocation, and a symlink-negative test exposed
ValueError from path containment. These were fixed before acceptance; no failure
was converted to skip/xfail or suppressed.

Validation outcome: 2,040 passed / 2 skipped in the global module/integration
run; after the shared-reader refinement, 327 focused tests passed separately on
Python 3.10 and 3.12, and 333 final structure/ownership/guide/weight checks passed.
Actual CCN and shape analysis passed for every changed Python file; Ruff passed.
All 79 actual packets round-tripped every path and byte. Largest: 98,570 tokens
(o200k_base), compile-answer-records. The context-tooling research update is also
part of its own packet. No production source changed. Full regression at the prior
production checkpoint c43455da finished with 16,405 passed / 244 skipped; that run
does not establish coverage of this test-tooling correction.
