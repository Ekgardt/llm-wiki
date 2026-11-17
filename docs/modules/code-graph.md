# Code graph — tree-sitter based code intelligence for the vault

`scripts/code_graph.py` is the command line and the public import surface. It
re-exports the names its consumers read and keeps the `--callers` command.

The answers live in six parts under `scripts/llm_wiki/`:

- `code_graph_calls.py` — callers and callees, stored or live;
- `code_graph_dead_code.py` — dead-code candidates and the reasons a symbol is kept;
- `code_graph_communities.py` — Louvain communities over the call graph;
- `code_graph_architecture.py` — entry points, hotspots, modules and routes;
- `code_graph_dependencies.py` — dependency walks and paths between symbols;
- `code_graph_flows.py` — argument flows and service paths.

They are built on the [code graph foundation](code-graph-foundation.md):
parsing, generations, the workspace call graph and answer reports.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
