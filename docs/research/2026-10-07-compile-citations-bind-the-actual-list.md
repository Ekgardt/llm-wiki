# Citation indices bind the actual evidence list

Research date: 2026-10-07. This qualifies the citation-count defect separately; it does not close the audit.

The retained real V21 repair returned 36 citations and an evidence index of 35.
The draft refused the evidence array at 32 and pruned claims above index 31.
Regression controls reproduce both refusals at 33, 36 and 64 citations. Empty
lists and invalid actual indices must continue to fail. The existing
`_claim_evidence_item` already rejects booleans, non-integers, negative indices
and indices outside the provided list. Citation byte/source validation remains
separate and mandatory.

Three independent primary sources inspected today:

- [JSON Schema array reference](https://json-schema.org/understanding-json-schema/reference/array): item validation and optional array length assertions are separate.
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs): schemas express array and integer constraints; it supplies no product requirement for 32 citations.
- [Google structured outputs](https://ai.google.dev/gemini-api/docs/structured-output): schema-conforming output still requires application value validation. The inspected page was updated 2026-09-23.

Alternatives: a larger fixed ceiling repeats an unqualified restriction;
splitting a page just to evade it distorts editorial meaning; a new setting
expands an unapproved environment/configuration contract. The selected change
removes the citation-count ceiling and its coupled integer maximum, while
keeping a nonempty typed array, nonnegative integer indices and exact actual-list
bounds at claim derivation. Existing response-byte, source-byte and after-image
checks, DLP, provenance and transactional publication remain. Independent source-accounting changes remain an uninstalled, separately qualified candidate.
This does not establish that every other existing resource limit is justified.

No path, runtime location, persistent schema or environment contract changes.
Draft identity changes to invalidate previous cached draft assumptions. The
tradeoff is that complete answers can carry more evidence and cost more tokens;
only a complete useful paired cycle can establish the resulting efficiency.
Passing this regression does not establish answer quality or point 7 completion.

The unchanged Root reproduction failed three valid cases and passed nine invalid-input controls. The fix passed all 183 related tests and 30 branch-shape checks under CPython 3.10.20. Four changed callables have measured maximum CCN 3, with no law-5 violations. The new shard weight was measured using the existing `tests.shard_plan --weigh` command, not guessed. Full-cycle source accounting remains unresolved and is not installed by this change.
