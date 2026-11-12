# Existing retrieval leaf owners

Date: 2026-11-03. Under the approved whole-project decomposition mandate, qualify
three existing modules in place: lane_score, temporal_anchor and evidence_sufficiency.
No production path or source byte changes; no retraining, new date interpretation,
threshold, cap, model dependency or runtime service. Pure tests move with unchanged
ASTs where mixed integration files currently import the entire query stack.

## Boundaries

lane_score owns only the shipped deterministic candidate score and text access.
The offline fitting command and retrieval ordering remain their actual callers;
no benchmark label is introduced at query time. Preserve all seven constants.
The existing pure score test moves out of test_one_score_over_the_lanes; its two
real retrieval-order tests stay. Full calibration history travels with the packet.

Temporal_anchor owns explicit day arithmetic, user-turn selection, date annotations
and search windows. Keep the exact existing distinctions between points and spans,
user and assistant text, and capture date versus query date. Its two existing pure
test files can be owned without moving them; isolated query expansion/window tests
can move out of mixed query integration files. Existing limits remain unchanged,
not newly justified or silently widened by this packaging work.

Evidence_sufficiency owns deterministic aspects of the best single evidence span,
its coverage value and explanatory text. It depends on temporal_anchor, never on
query orchestration, model calls or benchmark truth. Move the first three pure
coverage tests from the mixed refusal test file; keep actual query/refusal behavior
checks and all assertions in that file. This signal is not proof of answer truth.

## Current primary-source review

Three independent publishers checked on this date:
- Python date arithmetic and fixed civil-date representation:
  https://docs.python.org/3.10/library/datetime.html
- pytest package/import isolation and separate integration practices:
  https://docs.pytest.org/en/stable/explanation/goodpractices.html
- scikit-learn guidance on leakage and separation of training from held-out data:
  https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
These support preserving deterministic contracts and isolating tests; they do not
validate the project's fitted constants or empirical retrieval claims. Those remain
tied to the existing dated project evidence. Python 3.10 is upstream unsupported
but remains this project's compatibility requirement; no dependency is upgraded.

Alternatives: moving working source paths would create unnecessary aliases; merging
score, dates and coverage would couple independent semantics; importing the whole
query stack into leaf tests would conceal their actual dependency boundaries.
Choose in-place ownership and exact test transfer, retain separate integration gates.
Before changes, use the fresh built-in graph and source reads; record source hashes,
run existing tests, show missing-owner metadata failures, and verify unchanged ASTs.
Measure full dependency/test/history/instruction packets with exact reconstruction;
retain <=100000 tokens and all current source-closure gates. Run Python 3.12/3.10,
actual complexity, ownership and all module gates. Linux does not qualify native
Windows/macOS and no live installation is authorized.

## Qualification evidence

Fresh built-in generation generation-18e4779da35cf114-2340ffb8 binds 0f5508e3;
its graph confirms one candidate_score caller, 18 resolutions callers and five
sufficiency callers. The graph is partial; source reads established exact test
and dependency boundaries. The 61 original tests passed before transfer. Three
missing-packet checks failed with six existing-isolation guards already green.
Six moved test bodies and all retained test/helper bodies have identical ASTs;
all three production files and both in-place calendar test files retain their
original hashes. The nine new metadata/isolation checks bring the same consumer
selection to 70 passes on both Python 3.12 and Python 3.10. Actual CCN and shape
checks pass for every changed test function. No production function was changed.

All 74 packet artifacts reconstruct exact paths and bytes. Before this paragraph
lane score measured 65356, temporal anchor 71479, evidence sufficiency 72576 tokens;
the largest packet remains formats at 96703. This does not measure a task's LLM
usage. Ownership is 608/3314 tracked paths, with 2706 still unassigned.

Global module, source closure, ownership, documentation, privacy, limits and actual
complexity gates: 1812 passed, 2 skipped. The full regression is still running
on production checkpoint 0f5508e3; it does not yet include these test transfers.
