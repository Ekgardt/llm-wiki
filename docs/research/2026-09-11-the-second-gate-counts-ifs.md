# The second gate counts ifs

Date: 2026-09-11. Trigger: writing `scripts/contextual_retrieval.py` through
the Write tool, the managed rule-5 gate (`gate_complexity.py` of the
machine's enforcement set) refused it for "3 `if` statements at one level".
That gate runs on Write/Edit only. Every refactor this session was written
through Bash scripts and checked with `lizard -C 5` and
`~/.claude/tools/ccn_gate.py` (CCN, nesting, ternaries) — both passed — but
never with the managed analysis, which also enforces the rest of law 5's
text:

- more than two `if` statements in one block ("при появлении более двух if
  внутреннюю логику необходимо выделять в независимые подфункции");
- an `if` whose branch already exits but still carries an `else` (early
  return instead of if-else);
- radon cyclomatic complexity > 5 (radon counts some boolean operators that
  lizard does not);
- nesting > 2 and branching inside a conditional expression.

The read-only analysis (`gate_complexity.analyse`, run, never edited) over the
files this session touched found 157 findings: run_retrieval_v2 28,
lsp_security 30, pyright_profile 16, lsp_protocol 14, run_comparative 13,
run_code_navigation 10, run_scale_matrix 7, lsp_positions 7, sync_memory 3,
and one each in generate_python_qualification, doctor, capture_operation,
memory_queue; the 25 in impact_analysis are that file's legacy functions.

## Sources

1. Law 5 as written in `/home/user/.claude/CLAUDE.md`.
2. The managed gate's own rules (`MAX_IFS_PER_LEVEL = 2`, `MAX_NESTING = 2`,
   `MAX_COMPLEXITY = 5`, the guard-shaped if/else check).

## Decision

Every function written or modified this session is brought to zero findings
of the managed analysis, in addition to lizard and the ccn gate. The fix is
mechanical and behaviour-preserving: a chain of three guards becomes a guard
plus a named helper holding the other two; an exit-then-else loses its else;
a ladder of thresholds becomes a table. Messages and the order in which
checks fire are unchanged. Each batch is verified by the analysis, ruff and
the test files of the touched modules.

Files: `scripts/sync_memory.py`, `scripts/lsp_positions.py`,
`scripts/lsp_security.py`, `scripts/lsp_protocol.py`,
`scripts/pyright_profile.py`, `scripts/doctor.py`,
`scripts/capture_operation.py`, `scripts/memory_queue.py`,
`scripts/impact_analysis.py`, `benchmark/generate_python_qualification.py`,
`benchmark/run_retrieval_v2.py`, `benchmark/run_code_navigation.py`,
`benchmark/run_comparative.py`, `benchmark/run_scale_matrix.py`,
`CHANGELOG.md`.
