# Stored schema names remain SQL identifiers

Research date: 2026-09-29. Correction implemented; focused and migration checks passed.

The queue and Markdown coordinator inspect partially built candidate schemas.
Some names come from SQLite's schema, rather than the product's constant table
registry. Wrapping such a name in double quotes without escaping its embedded
quotes changes the SQL. In isolated in-memory databases, a populated table named
`safe" --` is reported empty by both probes when a different empty table named
`safe` exists. Both drop helpers remove `safe` instead of the requested name.
Related PRAGMA helpers fail on quoted names; two also reject spaces and keywords.
The initial probes produced 12 failures and four passing controls. No installed
database was modified, and no existing vault compromise is alleged.

Add one shared `quote_sqlite_identifier` primitive to the existing
`reliable_memory` module. It doubles embedded double quotes and wraps the entire
name. Apply it to the two population probes, both schema-object drop paths, and
all four table-column readers in these two modules. Statement keywords still
come from the existing fixed sets; row values continue to use bound parameters.
This does not authorize dropping a populated candidate: existing rebuild guards
and explicit adoption/maintenance boundaries remain in force.

Three independent primary sources inspected today:

- [SQLite printf, `%w`](https://www.sqlite.org/printf.html): embedded double
  quotes are doubled for a double-quoted identifier.
- [Python sqlite3](https://docs.python.org/3/library/sqlite3.html): use bound
  placeholders for values instead of constructing value text as SQL.
- [OWASP SQL Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html):
  table/column identifiers cannot be substituted as ordinary bind values; their
  construction requires a separate trust boundary.

Alternatives: reject every nonstandard identifier (unnecessarily refuses valid
SQLite names in partially built schemas), bind `?` in identifier positions (not
the SQL grammar), escape independently in each helper (duplicated security rule),
or add a new SQL library (unnecessary). A shared identifier encoder preserves the
exact existing name and needs no arbitrary name length or character restriction.
It is an implementation correction within existing modules, with no path,
runtime location, environment, persisted schema or ownership contract changes.

Installed SQLite is 3.45.1 through Python 3.12.3. The quoted-identifier behavior
is a longstanding supported SQL feature and introduces no newer Python API.
Regression fixtures use SQLite's own `%w` to create their reference objects,
independently of the new Python helper. This guards against an encoder and its
tests accidentally sharing the same escaping mistake.

Qualification must include actual wrong-table and populated-rebuild probes,
existing migration/adoption checks, Ruff and measured per-function complexity.
This finding prevents blanket dismissal of the remaining SQL warnings as
generated placeholders. No analyzer suppression is introduced.

## Verification and compatibility correction

The extended regression set also tests the real populated-candidate rebuild
guards: original code failed 14 checks and passed four. The first hardened
implementation passed 83 checks with three platform skips, including actual
Lizard/AST complexity measurement. Broader migration testing then found a genuine
compatibility regression: the old coordinator column-adder passed SQL-quoted
`operation` and `transaction` names to its reader, causing double encoding and
duplicate-column errors (80 failures, 74 passes, five skips). That failed run is
preserved.

The callers now consistently pass raw names; the common column-adder encodes both
table and column identifiers itself. Declaration text remains selected from
literal source definitions. No stripping heuristic or dual interpretation of
names was added. This removes the obsolete prequoted calling convention.
The complete rerun of new regressions, operational migrations, queue migrations,
adoption, Markdown coordinator and measured complexity passed 183 checks with
five platform skips in 44.19 seconds. Ruff passed. These results do not claim
Windows/macOS execution or certify the entire runtime.

Bandit continues to flag the interpolated queries (351 total findings, no parse
errors); that is expected because it does not evaluate the encoder. No warning
was hidden. Proof prefix:
`logs/audit-2026-09-29-completed-repair-sql-identifiers-`.
No live database repair, adoption, cleanup or deletion was invoked. Private
progress/log updates remain deferred during the running compiler snapshot.
