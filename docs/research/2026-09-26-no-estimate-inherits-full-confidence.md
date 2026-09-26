# No estimate inherits full confidence

Date: 2026-09-26. Audit 2026-09-26, finding C-10 (`vault_status` reports full
confidence with no compile history).

## What was wrong

`mcp_contract.build_envelope` defaults `coverage` and `confidence` to 1.0. A tool
result that no quality rule in `mcp_server._QUALITY_RULES` recognised kept that
default. `vault_status` had no rule, so a vault that had never compiled was
reported with full confidence, while the health resource, reading the same
fields, already said "Compile health is unknown" (`_compile_health_quality`).
Probed 2026-09-26 with an empty result: six of eleven tools were claimed by no
rule — `read_page`, `wiki_overview`, `vault_status`, `get_context`, `log_decision`
and `compile`; of these only `vault_status` answers with an estimate.

## Decision

- `vault_status` is judged by the health resource's own rule
  (`_quality_of_vault_status` → `_compile_health_quality`).
- `EXACT_ANSWER_TOOLS` names the tools whose successful answer is exact — a page
  read, a count of files, a write that happened or raised: `read_page`,
  `wiki_overview`, `log_decision`, `compile`, `get_context` (it reports its own
  missing pages as partial).
- Guard: a test probes every tool in `_TOOL_HANDLERS`; each must be claimed by a
  quality rule or be declared exact, so a new tool cannot inherit full confidence
  by being forgotten.

## Source

Saltzer and Schroeder's design principles, Wikipedia, fetched 2026-09-26 from
https://en.wikipedia.org/wiki/Saltzer_and_Schroeder%27s_design_principles:

- Fail-safe defaults: "Base access decisions on permission rather than exclusion."

Conclusion (mine): the same rule applied to confidence — a tool earns full
confidence by being declared exact, not by being left out of every rule.

## Files

- `scripts/mcp_server.py`
- `tests/test_no_estimate_inherits_full_confidence.py`
