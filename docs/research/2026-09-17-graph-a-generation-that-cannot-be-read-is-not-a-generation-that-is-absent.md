# A generation that cannot be read is not a generation that is absent

Date: 2026-09-17. Audit 3, code intelligence, finding B26 (the `code_graph` half;
`impact_analysis._active_graph` has the same shape and belongs to another area).

Files: `scripts/code_graph.py`,
`tests/test_a_generation_that_cannot_be_read_says_so.py`

## What was found

`code_graph._active_evidence_graph` ended in

```
    except (OSError, TypeError, ValueError, PermissionError, sqlite3.Error):
        return None
```

and every caller reads `None` as "this repository has no generation". Three
different things therefore arrived as the same answer:

1. the repository genuinely has no generation (the normal state of a fresh
   checkout);
2. the catalog or the generation database exists and cannot be opened — a
   truncated file, a permission change, `sqlite3.DatabaseError`;
3. a plain programming error inside the opening path, because `TypeError` was
   in the tuple. A wrong keyword or a `None` where a path was expected was
   caught and answered as "no generation".

In cases 2 and 3 the seven public entries of `code_graph` (`find_callers`,
`find_callees`, `find_dead_code`, `get_architecture`, `detect_communities`,
`find_dependencies`, `find_paths`) then parse the whole working tree live and
answer with `fallback: true`, `graph_complete: false` and nothing that says
why. `PermissionError` in that tuple is also redundant: it is a subclass of
`OSError`.

## Sources

- In-repository precedent, and the closest one there is:
  `tests/test_embedder_unavailable_names_its_reason.py`, whose docstring states
  the rule this fix applies — "Returning None is the right answer … but it was
  the *only* answer. A missing `sentence-transformers`, a model that was never
  downloaded, and a corrupt weight file were indistinguishable from one another
  and from a vault that simply had no vectors yet. … The contract does not
  change … What changes is that the reason has a name".
- Python documentation, `sqlite3` →
  [Exceptions](https://docs.python.org/3/library/sqlite3.html#sqlite3-exceptions)
  (fetched 2026-09-17): `DatabaseError` is "Exception raised for errors that are
  related to the database", and `DataError`, `OperationalError`, `IntegrityError`,
  `InternalError`, `ProgrammingError` and `NotSupportedError` are its
  subclasses; `ProgrammingError` is raised "for sqlite3 API programming errors,
  for example supplying the wrong number of bindings to a query". So
  `sqlite3.Error` covers both a damaged database and a caller's own mistake,
  which is one more reason the reason must be reported rather than erased.
- Python documentation, `exceptions` →
  [`TimeoutError`](https://docs.python.org/3/library/exceptions.html#TimeoutError):
  it is a subclass of `OSError`, which is why the existing `except TimeoutError:
  raise` clause must keep standing in front of the `OSError` clause. Likewise
  `PermissionError` is documented there as a subclass of `OSError`.

## Decision

- `TypeError` leaves the tuple. A `TypeError` in the opening path is a defect of
  this repository and must reach the caller.
- `PermissionError` leaves the tuple as redundant with `OSError`.
- What remains (`OSError`, `ValueError`, `sqlite3.Error`) is re-raised as
  `code_graph.GenerationUnreadable`, a `RuntimeError` carrying a bounded
  `reason` of the form `generation_unreadable:<ExceptionClass>`. The class name
  is the whole of it: no message, no path, nothing that could carry vault
  content into an answer.
- The seven public entries that own a live fallback catch it at the one place
  they already decide to fall back, and their report gains `fallback_reason`:
  `no_generation` when the repository has none, `generation_unreadable:<Class>`
  when it has one that could not be opened. `fallback` and `graph_complete`
  keep exactly their present meaning.
- The entries that have no live fallback (`find_argument_flows`,
  `find_service_paths`, and every caller outside this module) now see the
  exception instead of a `None` that means something else. That is the point:
  an unreadable generation is an operator-visible failure, not an answer.
