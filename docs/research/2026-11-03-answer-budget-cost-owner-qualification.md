# Existing answer shaping and cost telemetry owners

Date: 2026-11-03. Qualify scripts/answer_budget.py and scripts/answer_cost.py in
place under the approved completion mandate. Production bytes, defaults, public
imports, environment contract, report fields and response behavior stay unchanged.
The runtime byte estimate is not the o200k_base tokenizer used for maintenance
packets, and neither is a provider billing measurement.

## Boundaries and exact transfers

Answer_budget owns deterministic answer-tree transformations, the shared estimate,
protected fields, loss reporting and the existing named refusal. It depends only
on the standard library. Move the existing pure tests out of test_answer_budget.py;
MCP routing, argument validation and rendered-envelope checks stay in that file.
The shared table decoder should move once with a test-only import for the remaining
integration consumers, not be duplicated. Existing pure deep-tree and prefix tests
can be owned in place without edits. Keep all deep sharing/cycle and restoration
assertions. Preserve all historical constraints; no new cutoff or rank rule.

Answer_cost owns the existing optional telemetry block and depends downward on the
full answer_budget source and guide. Move its pure block tests and two fixture
helpers out of test_answer_cost.py; three actual MCP wiring/schema/failure tests
and their deadline helper remain. Preserve missing/zero distinction, stage states,
mode selection and self-cost accounting. The module does not claim actual provider
usage or adopt an OpenTelemetry SDK/schema.

## Primary sources, checked today

Three independent publishers:
- IETF RFC 8259, JSON value kinds and representation:
  https://www.rfc-editor.org/rfc/rfc8259
- Python 3.10 JSON serialization semantics:
  https://docs.python.org/3.10/library/json.html
- OpenTelemetry's current GenAI token metrics, now in its separate repository:
  https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-token-metrics.md
The old opentelemetry.io GenAI metrics URL is now a moved page. Current provider
usage conventions distinguish provider/billable counts and have specific rules
for absent responses; this is not permission to replace this product's existing
local-estimate/null contract or claim complete OTel conformance. No telemetry SDK,
new token estimator, tokenizer change or dependency upgrade is introduced.
Python 3.10 remains the project's compatibility requirement despite upstream EOL.

Alternatives: merging shaping with telemetry couples distinct responsibilities;
moving their production paths adds aliases without benefit; copying pure tests
would duplicate assertions and helper logic. Prefer unchanged owners and exact test
transfers. Keep dated research whole where required and measure the complete context
before acceptance; if it does not fit, refine a real boundary or exact record view,
never omit required assertions, source or authoritative instructions.

Before code: fresh built-in graph, source/hash baseline, original consumer tests,
missing-packet regressions. Compare moved and retained ASTs and production hashes;
run Python 3.12/3.10, real consumer tests, actual CCN/shape, all module/ownership gates
and full packet byte reconstruction <=100000 tokens. No live install and no claimed
full-cycle LLM improvement follow from this packaging work.

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
