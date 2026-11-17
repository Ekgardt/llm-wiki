# Code graph foundation — parsing, generations, workspace calls and reports

This module owns the four parts every code-graph answer is built on:

- `scripts/llm_wiki/code_graph_parse.py` loads the optional tree-sitter grammars,
  parses one file or a whole tree, and enriches Python calls with resolved targets.
- `scripts/llm_wiki/code_graph_generation.py` opens the active evidence generation,
  checks a caller's monotonic deadline, and reads stored rows (`stored_or_reason`,
  `stored_location`, `stored_qualified_name`, `stored_architecture_node`).
- `scripts/llm_wiki/code_graph_workspace.py` builds the live workspace call graph.
- `scripts/llm_wiki/code_graph_reports.py` writes the report every answer carries:
  what was read, what was cut and why (`live_report`, `store_report`,
  `seed_cut_report`, `reported_answer`, `dependency_reach`).

The answer modes (callers, callees, dead code, communities, architecture,
dependencies, paths and flows) live in the [code graph](code-graph.md) module and
read this one only through its public names. Tests replace
`code_graph_generation.active_evidence_graph` and
`code_graph_workspace.workspace_call_graph` on those modules, the one place every
answer looks them up.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.
