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


## Shared outer-whitespace policy, 2026-10-04

An actual closed journal reproduced physical-line refusal on 127 indented
Markdown bullet lines. Quote completion stripped outer whitespace before
recognizing a list marker; the final physical validator recognized that marker
before stripping. The two stages consequently disagreed about the same complete
line. The common `_without_bullet` now strips outer whitespace before applying
its existing unordered/ordered marker expression. The physical source bytes,
line bounds, hashes, complete-line comparison and evidence schema are unchanged.

Fresh primary research: [CommonMark 0.31.2 list
items](https://spec.commonmark.org/0.31.2/#list-items), [Python 3.10.22 regular
expressions](https://docs.python.org/3.10/library/re.html), and [Unicode UAX15
normalization](https://www.unicode.org/reports/tr15/) checked on 2026-10-04.
Anchored matching observes the supplied beginning; moving the already approved
outer-whitespace normalization before that match makes both stages agree.
Unicode normalization is deliberately absent: equivalent displayed NFC/NFD
strings are not identical physical bytes. A general Markdown renderer, citation
substring acceptance, and promotion of partial tool containers were rejected.

Five original indented-space/tab/ordered/unordered cases failed while six
controls passed. After the one-line shared fix, 58 related tests pass, including
exact UTF-8/NFD, blockquotes, unindented lists, incomplete citations, and a long
partial physical line. The same 127 actual source lines now pass the shared
validator without source changes or model calls. This does not attribute every
historical provider failure to this cause. Two large tool JSON containers still
cross physical compile parts; approved native user selectors do not extend tool
source authority. No settings, runtime paths, schema, or limits are added.
