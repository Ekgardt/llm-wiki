# Prefix exclusions are query data

Date: 2026-09-30. Status: installed and qualified.

The graph reader rejects more than 32 excluded name prefixes. The source
explicitly records no basis for that number. Every prefix currently creates
another SQL predicate and bound parameter, so merely removing the check would
eventually hit SQLite expression or parameter limits. The only production
caller supplies its existing dead-code naming conventions.

Sources checked today, from three independent primary publishers:

- [SQLite JSON functions](https://www.sqlite.org/json1.html): json_each reads
  an array as rows. The existing store already requires SQLite JSON support.
- [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html):
  parameter substitution keeps values separate from SQL statements.
- [OWASP SQL injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html):
  prepared statements preserve the code/data boundary.

Chosen design: bind the escaped prefixes as one JSON array and exclude matching
rows through json_each. No new schema, path, environment contract, dependency,
MCP tool or service is needed. Keep literal percent, underscore and backslash
escaping, the existing value validation, SQLite LIKE semantics and the exclusion
of null names when a nonempty prefix filter is supplied. Empty filters remain
unfiltered. Existing reader deadlines and row budgets still apply.

Alternatives: raising 32 leaves another unexplained threshold; deriving a
parameter ceiling still rejects a valid query because of its representation;
filtering already-limited results in Python would lose valid later rows. A
temporary table adds writes to the read-only reader. The chosen query adds
per-row JSON scanning; measure the real convention set before installation,
and retain refusal on deadline rather than promise unlimited resources.

Qualification must prove a prefix beyond the former boundary affects the answer,
SQL syntax in prefix values stays data, wildcard escaping and missing-name
semantics remain intact, and the original whole-graph regressions still pass.

Qualification: the two 33/1,025-prefix regressions failed on the previous
implementation. The repaired group passed 115 tests; the actual Lizard and
branch-shape guard passed 11 tests. Ruff passed. In five runs of each query
on the current repository generation, both returned the same 4,811 rows.
Median original time was 0.162 seconds; the JSON-array variant took 0.171
seconds. These measurements apply to the installed one-prefix convention,
not arbitrary large workloads. The existing caller deadline bounds expensive
queries. The source file was installed under the existing maintenance fence
with verified preimage and after hashes and a retained rollback copy.

Evidence: logs/audit-2026-09-30-continuation-prefix-red.txt, prefix-green.txt,
prefix-complexity.txt, prefix-measurement.json and prefix-activation.json
(the latter names use the same full prefix). The old constant and its
unsupported explanation were removed; no compatibility reader needs them.
