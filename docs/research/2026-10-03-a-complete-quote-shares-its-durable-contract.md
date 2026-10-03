# A complete quote shares its durable contract

2026-10-03. The installed automatic compile reports `$draft.operations[0].evidence[0].quoted_text: above maxLength`. The producer asks for one complete immutable source line; its draft schema and binding helper independently refuse more than 4000 characters while the existing durable claim ledger accepts literal text up to 16384. The current October 2 daily has 219 lines above 4000 characters, including captured tool JSON lines as long as 16226. This establishes a contradictory admission contract, not proof that every compile refusal has this cause.

The draft quote rule and binding helper now read the existing ledger literal rule. No ledger schema, runtime directory, environment setting, model output budget or source bytes change. Exact source digest, unique timestamp block, whole-line span, UTF-8 boundaries, literal hash and transaction/CAS checks remain mandatory. The draft program hash already includes its schema, so an old cache entry cannot masquerade as a result of this new prompt.

Current primary sources checked on 2026-10-03:

- [JSON Schema string reference](https://json-schema.org/understanding-json-schema/reference/string): `maxLength` is a declared string constraint; it does not make 4000 a universal JSON capacity.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html): implementations can set resource limits; the interoperability contract and actual implementation bounds must be distinguished.
- [SQLite implementation limits](https://www.sqlite.org/limits.html): database string/row limits are explicit byte limits and are not a 4000-character field capacity. No SQLite setting is changed.

Alternatives rejected: truncating a citation destroys complete-line evidence; retrying the same immutable input cannot fix incompatible schemas; introducing a new configurable quote ceiling duplicates the existing durable consumer contract; removing every bound before checking consumers could create unqualified downstream failures. Sharing the actual consumer rule is the smallest compatible correction.

Three new regressions fail on the old code. Two submit complete 4023/12022-character lines through draft validation, immutable binding, a recoverable page commit and citation resolution. The third detects future divergence from the ledger literal rule. Existing fabricated/ambiguous-source regressions are retained.

Limits of this result: the existing ledger 16384-character contract is retained, not newly established as a measured optimal budget. Eight current daily lines exceed it and require separate investigation, including compile-part boundaries. A successful fixture commit is not a successful whole live model compile or full token-cost qualification; the running compiler loaded its earlier code before this installation. The four open audit items remain open until their full operational checks pass.

Evidence: ignored local logs `audit-2026-10-03-complete-quote-*.json` and `audit-2026-10-03-compile-complete-line-lengths.json`, plus the regression tests. Private source text is not published.
