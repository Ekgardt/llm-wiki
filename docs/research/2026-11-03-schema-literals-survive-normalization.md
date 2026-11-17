# Schema comparison must preserve SQL literal values

Date: 2026-11-03. Reproduced defect found before the coordinator schema transfer.

Both markdown_transaction._normalized_coordinator_sql and
queue_schema_operations._normalized_sql collapse whitespace and casefold the
entire SQL string. A real in-memory coordinator with the transaction CHECK value
'preparing' changed to 'PREPARING' passes _coordinator_v3_schema_complete, yet an
ordinary 'preparing' insertion fails SQLite's CHECK. A queue tasks schema with
'ready' changed to 'READY' likewise passes its complete-schema check. These are
changed constraints accepted as the original managed contract.

Correct the shared comparison operation before further extraction. A small
stdlib lexical normalizer preserves complete quoted tokens (single/double/backtick
and bracket forms) and comments, including line-comment terminators. Only outside
those tokens does it fold ASCII upper-case syntax and collapse ASCII whitespace (space, tab, CR, LF and form feed). Preserve
non-ASCII characters instead of Unicode casefolding identifiers into other names.
The output is comparison text, never executed SQL; this is not a SQL parser or a
claim of arbitrary SQL semantic equivalence. Both existing helper names delegate
to this one implementation. Managed CREATE text, schema hashes, versions and all
migration data remain unchanged.

Preserving quoted identifiers is conservative because SQLite double quotes can
also denote string literals in compatibility cases. Generated historical schemas
use the canonical quoted spelling. The comparator still accepts existing ASCII
syntax/whitespace variations outside protected tokens; changed literal contents
must fail. Comments keep their contents and line endings so a collapsed newline
cannot turn a comment into a different token boundary. Unknown malformed SQL is
not repaired or rewritten for execution.

Alternatives: exact SQL byte equality would unnecessarily reject outside-token
formatting; retaining global casefold accepts changed constraints; a general SQL
parser adds a new dependency and dialect assumptions. The selected lexical helper
has no connection/filesystem ownership and uses only Python 3.10 stdlib. Its
qualification includes escaped quotes, literal whitespace, Unicode identifiers,
comments and line terminators, old managed schema fixtures and actual validators.
Retain all existing tests, add real-SQLite regressions that fail before the fix,
verify positive legacy schema cases, CCN and every complete packet budget.

Primary sources checked today (three independent publishers):
- SQLite literal and identifier quoting rules, including quote doubling and DQS
  compatibility: https://www.sqlite.org/lang_expr.html and
  https://www.sqlite.org/lang_keywords.html
- Python regular-expression grouping/substitution contract:
  https://docs.python.org/3.10/library/re.html
- OWASP structured-input allowlist validation:
  https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
This enforces the existing managed-schema comparison intent. It does not change
runtime roots, operational schema versions, dependency/support contracts or install
anything into the live vault. Evidence: private
logs/schema-literal-normalization-reproduction.json. Native CI remains pending.
