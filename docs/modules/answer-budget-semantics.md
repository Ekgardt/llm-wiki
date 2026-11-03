# Answer budget public semantics — experimental contract

Status: reviewed against d25fac49 source for the dependency-contract pilot; not
qualified for source omission. Production code and both existing full-source
packets remain authoritative. Interface declarations will be checked against
actual provider definitions, values and runtime bindings before adoption.

## Values and state

The public locally defined API consists of `estimate_text_tokens`,
`estimate_tokens`, `shape_code_answer`, and the constants in the accompanying
interface. Imported implementation modules are not part of this declared API.
Private names and imported implementation objects cannot be accessed through this
contract. Constants are read-only configuration of the implementation, not caller
mutation points. No database, file, network, subprocess, clock, persistent cache,
lease or transaction is owned by these functions. They have no deadline parameter
or cancellation guarantee. Work and memory depend on the supplied answer.

Inputs are ordinary trusted Python JSON-like values. Custom object methods,
including `__str__` and overridden container operations, may execute caller code.
Thus arbitrary objects are not an untrusted-code sandbox or a promise of purity.
No exception from caller object methods is suppressed by the provider.

## Estimation

`estimate_text_tokens(text)` returns the ceiling of the UTF-8 encoded byte length
divided by `BYTES_PER_TOKEN` (currently 2). Empty text returns zero. Encoding uses
strict errors; lone surrogates can raise UnicodeEncodeError. A value without a
compatible `encode` method fails normally. This is an offline estimate, not a
model tokenizer, usage counter or billing number.

`estimate_tokens(data)` first uses `json.dumps(data, ensure_ascii=False,
default=str)` with all other Python defaults, then the text estimate above.
Insertion order and default separator spaces matter. Non-ASCII is not ASCII
escaped. Non-finite numbers retain the standard JSON encoder's default handling.
Unsupported value objects are stringified; unsupported mapping keys still raise
TypeError. Circular references raise ValueError; excessive serialization recursion
can raise RecursionError. Stringification and UTF-8 encoding errors propagate.
The estimate does not mutate ordinary input containers.

## Shaping order and output

`shape_code_answer(data, *, budget_tokens=None, include_node_ids=False)` returns
non-dictionaries unchanged, without validating a supplied budget. Dictionaries
pass through these ordered steps: opaque/recoverable identity removal unless
include_node_ids is truthy; redundant module-name shortening; profitable row
constant extraction; budget fitting; profitable prefix/column encoding. Changes
are represented in the returned tree. Ordinary input containers are not mutated.
Shared acyclic children are accepted and rewritten independently. Encountered
container cycles raise ValueError. JSON estimation may still raise recursion or
serialization errors; there is no promise that arbitrary-depth input serializes.

Opaque scalar IDs match the complete lowercase form `code:[a-z]+:[0-9a-f]{32}`.
Nonempty nested lists whose leaves all match are also removable. Fields named in
PROTECTED_FIELDS are spared this removal. Removed field names are collected.
A repository identity starts with `repository:` plus 64 lowercase hex digits and
a unit separator. Its readable segments after the repository and language tags
are removable only when all are already among stringified values of that row's
metadata dictionary. Setting include_node_ids preserves both kinds of identity.

Module-name shortening uses the first nonempty string path among relative_path,
path, file, and the first name among qualified_name, name. Paths normalize slashes,
strip a terminal `.py`, and become dotted components. The longest qualified-name
prefix matching the path suffix is removed, leaving the symbol/class suffix.
This applies independently of identity removal and budget pressure.

A constant table contains at least two dictionaries. Columns present in all rows
with identical scalar values (including identical Python types, so False differs
from 0) move into `<key>_row_constants` only when the full candidate representation
estimates cheaper. Constants are removed from individual rows but retained in that
sibling mapping. Traversal visits dictionaries and lists.

## Budget fitting

Explicit budgets must be Python integers, excluding bool, in the inclusive
MIN_BUDGET_TOKENS..MAX_BUDGET_TOKENS range (currently32..25000), otherwise ValueError.
A missing budget uses DEFAULT_BUDGET_TOKENS (25000). REPORT_TOKEN_ALLOWANCE (96)
is reserved before fitting the body. A default-budget answer already below that
body threshold receives only an omitted_fields report when fields were removed;
otherwise it has no added report. Explicit fitting reports even when no reduction
is necessary.

Under pressure, DERIVABLE_FIELDS (owner) are removed first, then rows are removed
from tails of nonempty all-dictionary lists. Lists are considered largest first
with stable traversal order for equal lengths. Each removal batch uses estimated
overflow divided by mean row cost, floored to at least one and capped at the
remaining row count; fitting re-estimates after each batch. This is the current
behavior, not a promise to preserve the maximal possible number of rows.

A successful explicit/fallback fit adds answer_budget containing budget_tokens
and body_tokens (before the report and final column encoding), optional sorted
unique omitted_fields, and rows_omitted plus truncated:true if any rows were lost.
An existing answer_budget field can be replaced by this generated report.
If the body still cannot fit, output is a named refusal: status:error,
error:answer_budget_too_small, and answer_budget with budget_tokens,
minimum_tokens (remaining body estimate plus allowance), rows_omitted,
truncated:false and the explanatory note. No silently shortened successful answer
is returned in that case.

## Final representation

After budgeting, dictionary traversal treats lists as opaque. Uniform tables
(at least two dictionaries with exactly the same keys) can shorten string columns
by their longest shared prefix ending in `/`. Prefixes are retained in
`<key>_row_prefixes`, only when the complete prefixed representation is cheaper.
Then sorted column names can move into `<key>_cols` and row dictionaries become
arrays, again only when cheaper. Prefixes and constants are needed to reconstruct
rows. Reserved sibling keys can be overwritten by generated representation keys;
callers must not assume arbitrary conflicting keys survive. No new collision
handling is introduced by this contract.

Provider regression tests cover restoration, identity preservation/removal, deep
trees, cycles, shared children, default/explicit/refusal budgets, constants and
prefixes. The consumer pilot additionally checks the real estimator binding and
ordinary, Unicode, unsupported and circular serialization behavior. Finite tests
are qualification evidence, not a proof of all Python behavior.
