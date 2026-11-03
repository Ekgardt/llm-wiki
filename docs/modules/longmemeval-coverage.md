# LongMemEval evidence coverage

Owns benchmark/longmemeval_coverage.py and the complete original
tests/test_longmemeval_coverage.py in place, byte-identical. Functions normalize
supplied text, match labelled sessions and evidence turns, compute coverage for
ranked prefixes and aggregate counts by depth/type. They also distinguish missing
judge verdicts from wrong answers and count the compiler's requested/missed chunks.
They do not retrieve, call a model, run a worker or publish a report.

The existing session-name substring matching, whitespace/case normalization,
80-character windows at 40-character steps, shorter-turn matching and default
12/24/48 depths remain unchanged. These are existing heuristics, not semantic
proof that evidence is sufficient. Counts, rounding, None for absent denominators,
false all-evidence flags for absent labels and error behavior are preserved.
Custom Mapping/Sequence objects can execute their methods; this is not a sandbox.

Fourteen original coverage tests remain in their original file. Three pure
prompt/compile coverage assertions move unchanged from the mixed report suite.
Their three synthetic fixture constants have one location under this owner's test
package and are imported by the retained worker integrations. All nine remaining
scoring/worker/report tests stay in the original mixed file. No assertion is removed.
Both complete September 14/18 records remain shared context; they include broader
benchmark decisions and do not become current product-quality claims.

Run tests/test_longmemeval_coverage.py, tests/modules/longmemeval_coverage and
tests/test_the_report_says_how_much_evidence_reached_the_reader.py with normal
pytest bootstrap. The packet retains source, tests, fixtures, instructions and
history below 100000 o200k_base tokens, with exact file reconstruction. Fresh-process
import guards include scripts and benchmark roots and refuse executor dependencies.
Python 3.14 is the approved runtime; there is no installation or dependency change.

## Qualification research — 2026-11-03

The current built-in graph at 5b43b7e5 and full source/import search confirm the
value computations and their worker/report consumers. Baseline 26 tests pass.
Preserve the existing cohesive module; merging it into the execution layer would
broaden dependencies, and arbitrary splitting or a new protocol framework offers
no demonstrated benefit. Source, default depths, formats and caller contracts stay.

Primary sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[Python container interfaces](https://docs.python.org/3.14/library/collections.abc.html).
These support explicit responsibilities and container semantics, not new claims
about the historical retrieval measurements or full-cycle token efficiency.
