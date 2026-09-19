# A cut walk is not "no path"

Dated 2026-09-17. Third audit, finding G-M7 (first point). The research before the fix.

Files: `scripts/evidence_graph.py`, `tests/test_a_cut_walk_is_not_no_path.py`.

## What was found

- `EvidenceGraph.path` walks with a recursive CTE whose `LIMIT` is `max_work + 2`, selects the
  rows that end at the target, and reads the walk's size from a column of those rows:
  `if rows and rows[0]["work_count"] - 1 > work_limit: raise ...`.
- When the limit stops the walk before it reaches the target, no row ends at the target, so
  `rows` is empty, the size is never read, and the caller gets `[]`. `code_graph.find_paths`
  (`max_work=10_000`) reports that as "no path". A truncated search reads as a complete
  negative.
- Reproduced on the five-node test graph: `caller -> callee -> branch-c` is found with
  `max_work=20` and answered `[]` with `max_work=2`.
- The sibling `_neighbors` cannot hit this: it selects every reached node, so a cut walk always
  returns rows that carry the count.

## Practice on this date

- SQLite, "The WITH Clause": "The LIMIT clause, if present, determines the maximum number of
  rows that will ever be added to the recursive table in step 2b. Once the limit is reached,
  the recursion stops." (https://www.sqlite.org/lang_with.html, fetched 2026-09-17). The
  stop is silent by design; the only evidence of it is the table's size, so the size has to
  be read whether or not anything matched.
- The module's own rule for every other bounded read: "the work ceiling refuses rather than
  truncates" (`neighbors` docstring).

## The decision

- The size of the walk is selected on its own and the matching rows are joined to it, so one
  row always carries `work_count`. The ceiling is checked first; rows without a path are then
  dropped. Same statement count, same ordering, same error text as the existing refusal.
- The other two points of M7 (`TimeoutError` missing from three `except` tuples;
  `_published_database` validating without a deadline) are not changed here.
