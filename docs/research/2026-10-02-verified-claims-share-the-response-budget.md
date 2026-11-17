# Verified claims share the response budget

Date: 2026-10-02. Scope: optional source-bound claims in compile output. No database format, paths, environment contracts, providers, or runtime locations change.

The provider schema permitted eight candidates per page; `_within_the_claim_cap` also dropped every admitted candidate after the eighth. Its comment explicitly said the numeric basis was unknown. A small source-backed plan with nine distinct lease statements reproduced both defects: native schema rejection and normalization keeping only eight records with an explicit ninth-claim drop. The original run failed both checks.

## Research and alternatives

Three independent primary sources were checked on this date:

- [JSON Schema arrays](https://json-schema.org/understanding-json-schema/reference/array): item validation and a separate `maxItems` count are independent controls. Removing a count does not remove the item schema.
- [Python JSON](https://docs.python.org/3/library/json.html): untrusted JSON requires resource controls before expensive processing. The current documentation is Python 3.14; the JSON operations used here remain compatible with the supported Python 3.10 baseline.
- [OWASP API resource consumption](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/): limits should correspond to actual resource consumption. A claim count is an indirect proxy when the complete response already has an enforced byte budget.

Making eight configurable preserves an unexplained data-loss rule. Raising eight to another unmeasured count repeats it. Dropping provenance verification would admit invented claims. The selected change removes only the separate eight-claim maximum from the provider schema and admission path, and deletes its replaced private helper and constant. Every candidate still passes the existing schema. Derived records retain exact immutable byte spans and hashes, canonical semantics, the page ledger schema, contradiction handling, and recoverable publication. The complete response remains bounded by the existing 4 MiB response check and the final page by its existing byte budget; this change does not certify the optimality of those other limits.

Larger correct pages may contain more candidate facts, so downstream work may increase; the whole-response budget and existing operational deadlines continue to apply. There is no new model call introduced by admission. Full-cycle answer and token qualification remains a separate unfinished audit obligation.

## Verification

The strengthened regression normalizes all nine claims, validates every evidence span, commits the page through the real coordinator, rebuilds the real claim index, and retrieves each distinct subject with its exact quoted line. The provider schema admits the same complete plan. Related compile, claim, transaction, malformed-claim, fabricated-quote and oversized-response checks: 126 passed. Actual Lizard: changed admission CCN 4; new regression functions CCN 1–5. Private evidence is recorded under `logs/audit-2026-10-02-claim-budget-*`.

The installed producer additionally committed all nine records and the real claim index retrieved every subject on a separate adopted v3 temporary vault, in 0.468 s with zero model calls. The retained older cut-reporting regression was updated to the deliberate new contract: all ten valid candidates survive and only the malformed candidate is reported as dropped. Its original implementation fails this strengthened assertion. Together, the three reproduced failures are preserved in the complete red log. The old cap constant and helper have no remaining production or test references.

Final combined verification, including the updated older regression and all mandatory guards: 195 passed in 60.71 s. Final Ruff and Gitleaks checks passed. The changed older regression measured CCN 3.
