# Context lists use the existing budget

Date: 2026-09-30. Status: installed and qualified.

get_context refuses more than 20 requested pages, more than 10 compatibility
include values, or an include string longer than 64 characters. The constants
explicitly have no measured basis. The implementation collects the same bounded
corpus before selecting requested pages, and include does not select content.
Those limits therefore do not protect source reading or improve context quality.

Primary sources checked today:

- [JSON Schema arrays](https://json-schema.org/understanding-json-schema/reference/array):
  item types, nonempty arrays and uniqueness are independent of maxItems.
- [Python 3.10 collections](https://docs.python.org/3.10/tutorial/datastructures.html):
  dictionary/set membership can deduplicate and select a finite request list.
- [MCP tool contracts](https://modelcontextprotocol.io/specification/2025-06-18/server/tools):
  input schemas define caller-visible validation; no 20-page limit is prescribed.

Remove only those three redundant bounds from direct validation and the published
schema. Preserve nonempty typed slug lists, identifier validation, unique items
on the MCP surface, string-only compatibility lists and the existing caller
token budget. Source collection remains protected by its existing configurable
source/count/byte budgets and the operation deadline. The complete context
package, including requested/missing page metadata and compatibility metadata,
must still fit the requested budget or refuse by name. This does not create
a transport-wide input allocation guarantee: the SDK has already parsed arguments
before this validator runs.

Alternatives: larger numbers leave unexplained restrictions; introducing new
list-size settings duplicates the actual corpus and answer budgets; dropping
unknown slugs or include values silently changes caller-visible accounting.
Removing include entirely breaks its existing compatibility contract. Keep
that contract and revisit removal only after checking real consumers.

Qualification must cover a real 21-page context package, more than ten include
values and a long compatibility string, direct and schema validation, whole
answer budget refusal, invalid types, collection deadlines and complexity.
No new files of configuration, environment variables, schema fields or runtime
location are introduced.

The two regressions failed before repair. After correcting a test's expected
missing-page order to the established lexical order, the related group passed
604 tests (4 skipped). Stronger exact-content coverage and the real complexity
guard passed another 33 tests: all 21 page facts survived in the text at the
existing 8,192-token allowance; oversized compatibility metadata refused by
name. Ruff passed. The source was installed under the existing maintenance
fence with verified preimage and after hashes and a retained rollback copy.
The three constants and the unused include-bound helper were removed.

Evidence: logs/audit-2026-09-30-finish-context-red.txt,
logs/audit-2026-09-30-finish-context-final-green.txt,
logs/audit-2026-09-30-finish-context-facts-and-complexity.txt and
logs/audit-2026-09-30-finish-context-activation.json.
