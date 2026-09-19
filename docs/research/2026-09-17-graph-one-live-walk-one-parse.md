# One live walk, one parse, and a block that has an end

Date: 2026-09-17. Audit 3, code intelligence, findings B27, B24 and the C7 item
"the regex pass in `_regex_parse_python` computes calls/imports and discards
them".

Files: `scripts/code_graph.py`, `scripts/import_resolver.py`,
`tests/test_one_live_walk_one_parse.py`, `tests/test_code_graph.py`

## What was found (each verified by running it)

1. **B27, three walkers.** The live fallback walks the workspace in three
   places with three rules: `_INDEX_SKIP_DIRS` (`index_directory`),
   `_SEARCH_SKIP_PARTS` (live callers and callees; it does not even name
   `venv`) and `_parsable_workspace_file` (the call graph; the only one that
   prunes hidden directories). So `find_callers(live=True)` still parses every
   `.claude/` agent worktree, which NEW-110 measured at 94% of the parsable
   files of the live vault.
2. **B27, absolute parts.** All three test `path.parts` of the *absolute* path.
   A repository checked out at `/x/venv/proj` yields zero definitions, because
   the word `venv` is in the path of every file of it.
3. **B27, two parses.** Live `callers`, `callees` and `communities` call
   `_live_report(directory)` without the list they have just parsed, so
   `_live_unresolved_count` parses the whole workspace a second time.
4. **B24, a function without an end.** When a tree-sitter grammar is absent the
   regex fallback writes `end_line = line` for every function and class.
   `_callees_in_function` keeps a call only when `line <= call <= end_line`, so
   live `find_callees` answers `[]`; `_containing_function` finds no caller for
   any call, so no edge is resolved and live `find_dead_code` names called
   functions as dead. The answer is wrong, not merely coarse.
5. **C7.** `_regex_parse_python` runs three regular expressions over every
   line, collects calls and imports, and then overwrites both with the
   resolver's result. Only the definitions of that pass are used.

## Sources

- Python standard library, `os.walk` docstring (read from the project
  interpreter, 3.14): "When topdown is true, the caller can modify the dirnames
  list in-place (e.g., via del or slice assignment), and walk will only recurse
  into the subdirectories whose names remain in dirnames; this can be used to
  prune the search, or to impose a specific order of visiting." Pruning at the
  directory is what makes the rule relative by construction: a name is judged
  only when it is met *below* the root, never on the way to it. `rglob("*")`
  followed by a filter lists all 7,261 files under `.claude/` before dropping
  them.
- The Python Language Reference, "Compound statements"
  (https://docs.python.org/3/reference/compound_stmts.html, fetched
  2026-09-17): "Compound statements contain (groups of) other statements; they
  affect or control the execution of those other statements in some way." A
  Python block is delimited by indentation, so the end of a `def` is the last
  line before the next non-blank line indented no deeper than the `def`
  itself. Brace languages close a block where the brace depth opened by the
  declaration returns to zero.
- In-repository precedent: `import_resolver._workspace_python_files` already
  walks with `os.walk`, prunes `dirnames` in place and carries the one rule
  ("hidden, or `node_modules`/`venv`/`__pycache__`").

## Decision

- One rule, the one `import_resolver` already owns, made public as
  `directory_skipped`. One walker in `code_graph`, `_live_source_files`, built
  on `os.walk` with in-place pruning; `index_directory`, live callers, live
  callees and the workspace call graph all use it. The three skip sets and the
  three predicates go away.
- Live callers and callees parse once and hand the parsed list to
  `_live_report`; live communities hand over the list the call graph already
  produced.
- B24: the regex fallback computes the end of a block: by indentation for
  Python and Ruby, by brace depth for the brace languages, bounded by the file.
  The alternative - refuse `callees` and `dead_code` on the regex path - was
  rejected: the fallback exists so that a machine without a grammar still gets
  an answer, and the heuristic end is as good as the heuristic start it
  already reports. A declaration whose block never opens keeps `end_line =
  line`.
- C7: the Python regex pass collects definitions only.
