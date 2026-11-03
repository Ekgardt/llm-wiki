# Offline lane fitting as an existing owner

Date: 2026-11-03. Proposed after the benchmark-context closure correction.
Qualify benchmark/fit_lane_score.py in place with unchanged bytes, parameters,
public CLI, float operation order and output text. It consumes recorded JSON/JSONL
and reports a proposed fit; it neither runs a benchmark nor modifies shipped
constants. No real-data refit or provider request is part of qualification.

Move its existing synthetic-file tests and their helpers from the mixed refit test
file into its packet. Keep the actual longmemeval_vault.lane_matrix recorder test
in place with all assertions. Move the two fitter/constant-name pin tests from the
mixed reader-window file; its two reader-window checks remain there. Preserve
all transferred function ASTs and helper values. The full lane_score dependency,
its canonical guide and dated fit/refusal histories belong in the context.

Three independent primary publishers checked today:
- NumPy array conversion and explicit dtype:
  https://numpy.org/doc/stable/reference/generated/numpy.asarray.html
- scikit-learn grouped cross-validation and leakage cautions:
  https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators-for-grouped-data
- pytest import/package isolation:
  https://docs.pytest.org/en/stable/explanation/pythonpath.html
The grouping principle applies to the existing question-level folds; this does
not validate a new model or authorize using held-out data to tune it. The code
uses NumPy, not scikit-learn. Current Python 3.12 environment has NumPy 2.5.1,
pytest 9.0.3 and tiktoken 0.14.0; check the existing Python 3.10 environment too.
No dependency is upgraded for this qualification.

Alternatives: moving the production CLI would break needless paths; combining
runtime score and offline fitting would obscure their authority and label boundary;
keeping the recorder executor inside pure fitter tests would import unrelated
product state. Choose unchanged source and exact test transfer. Add explicit static
and isolated-import guards, including lazy lane_score/NumPy dependencies. Prove the
stronger benchmark-aware packet closure and all original synthetic regressions,
retained recorder/reader integrations, Python 3.10, actual changed-code complexity,
ownership and <=100000-token exact reconstruction. Fixture-fit success is not an
improved ranking measurement, and Linux is not native Windows/macOS qualification.

The shared test-only import guard will accept explicit source roots for this CLI
(scripts and benchmark), while its default remains scripts. Both roots contribute
to the guarded local-module set; adding a benchmark root must not permit another
benchmark module unless named as a dependency. Extend the common helper and its
negative subprocess test rather than copying a second guard. Python 3.10's existing
environment uses NumPy 2.2.6 and pytest 9.0.3; keep both environments unchanged.

## Qualification evidence

The combined baseline passed 126 tests. Missing packet metadata first failed all
three budget checks; the cross-root import guard first failed both negative cases
before the shared helper was corrected. The completed owner and retained consumer
selection passed 147 tests on Python 3.12 and 147 on Python 3.10. All 52 transferred
function ASTs, retained function ASTs, three production files and two existing
pure test files were verified unchanged. Actual changed-code CCN and shape checks
and Ruff passed. The full module, ownership, structure and policy selection passed
1895 tests with 2 skips. All 77 serialized packets reconstructed every path and byte
and fit the 100000-token ceiling. Evidence is retained in the private logs as
answer-fit-owners-green.txt, answer-fit-py310.txt, answer-fit-global.txt,
answer-fit-transfer-proof.json and context-bundle-comparison.json.

Whole-project ownership remains incomplete: 643 of 3336 inventoried files have
whole-file owners; 2693 remain unassigned. The full regression at the preceding
queue-evidence checkpoint 0f5508e3 passed 16285 tests with 244 skips and 5 warnings;
it does not qualify these later test moves. Native Windows/macOS CI remains
unverified because workflow dispatch lacks repository admin permission. No live
installation or provider benchmark was run, and no full-cycle LLM savings are
claimed.
