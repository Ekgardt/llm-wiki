"""Code graph — tree-sitter based code intelligence for the vault.

Parses source code files into a knowledge graph: functions, classes,
imports, call edges. Stores results for graph queries and impact analysis.

The graph enables:
- "Who calls this function?" — caller analysis
- "What will break if I change this?" — impact analysis (blast radius)
- "Find dead code" — functions with zero callers

Languages: Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, Ruby,
PHP, C#, and Bash. Each grammar is optional and loaded only when needed.

Install: uv sync --locked --inexact --extra code-graph
"""
from __future__ import annotations

from pathlib import Path

try:
    from .llm_wiki.code_graph_architecture import (
        HOTSPOT_LIMIT,
        SUMMARY_LISTING_LIMIT,
        get_architecture,
        hotspot_fields,
        listing_fields,
        module_fields,
        stored_modules,
        summary_listing_limit,
    )
    from .llm_wiki.code_graph_calls import (
        find_callees,
        find_callers,
        stored_callers,
    )
    from .llm_wiki.code_graph_communities import (
        COMMUNITY_LIMIT,
        MAX_DERIVED_COMMUNITY_CACHE,
        aggregate_louvain_graph,
        detect_communities,
        louvain_communities,
        stored_communities,
        undirected_call_graph,
    )
    from .llm_wiki.code_graph_dead_code import (
        DEAD_CODE_REASON_ORDER,
        dead_candidate_row,
        dead_code_counts,
        find_dead_code,
        ordered_dead_candidates,
        stored_dead_candidates,
    )
    from .llm_wiki.code_graph_dependencies import (
        DEPENDENCY_DEFAULT_DEPTH,
        DEPENDENCY_MAX_DEPTH,
        PATH_MAX_ROWS,
        dependency_seed_nodes,
        find_dependencies,
        find_paths,
        paths_between,
        stored_dependency_rows,
    )
    from .llm_wiki.code_graph_flows import (
        FLOW_MAX_ROWS,
        find_argument_flows,
        find_service_paths,
        flow_cut_report,
    )
    from .llm_wiki.code_graph_generation import (
        GenerationUnreadable,
        active_evidence_graph,
        generation_catalog,
    )
    from .llm_wiki.code_graph_parse import (
        LANGUAGE_MAP,
        detect_language,
        enrich_python_semantics,
        enriched_call,
        index_directory,
        live_source_files,
        parse_file,
        parse_file_in_workspace,
        parser_attempt,
        tree_sitter_parsers,
    )
    from .llm_wiki.code_graph_reports import (
        dependency_reach,
    )
except ImportError:
    from llm_wiki.code_graph_architecture import (
        HOTSPOT_LIMIT,
        SUMMARY_LISTING_LIMIT,
        get_architecture,
        hotspot_fields,
        listing_fields,
        module_fields,
        stored_modules,
        summary_listing_limit,
    )
    from llm_wiki.code_graph_calls import (
        find_callees,
        find_callers,
        stored_callers,
    )
    from llm_wiki.code_graph_communities import (
        COMMUNITY_LIMIT,
        MAX_DERIVED_COMMUNITY_CACHE,
        aggregate_louvain_graph,
        detect_communities,
        louvain_communities,
        stored_communities,
        undirected_call_graph,
    )
    from llm_wiki.code_graph_dead_code import (
        DEAD_CODE_REASON_ORDER,
        dead_candidate_row,
        dead_code_counts,
        find_dead_code,
        ordered_dead_candidates,
        stored_dead_candidates,
    )
    from llm_wiki.code_graph_dependencies import (
        DEPENDENCY_DEFAULT_DEPTH,
        DEPENDENCY_MAX_DEPTH,
        PATH_MAX_ROWS,
        dependency_seed_nodes,
        find_dependencies,
        find_paths,
        paths_between,
        stored_dependency_rows,
    )
    from llm_wiki.code_graph_flows import (
        FLOW_MAX_ROWS,
        find_argument_flows,
        find_service_paths,
        flow_cut_report,
    )
    from llm_wiki.code_graph_generation import (
        GenerationUnreadable,
        active_evidence_graph,
        generation_catalog,
    )
    from llm_wiki.code_graph_parse import (
        LANGUAGE_MAP,
        detect_language,
        enrich_python_semantics,
        enriched_call,
        index_directory,
        live_source_files,
        parse_file,
        parse_file_in_workspace,
        parser_attempt,
        tree_sitter_parsers,
    )
    from llm_wiki.code_graph_reports import (
        dependency_reach,
    )

__all__ = [
    "COMMUNITY_LIMIT",
    "DEAD_CODE_REASON_ORDER",
    "DEPENDENCY_DEFAULT_DEPTH",
    "DEPENDENCY_MAX_DEPTH",
    "FLOW_MAX_ROWS",
    "GenerationUnreadable",
    "HOTSPOT_LIMIT",
    "LANGUAGE_MAP",
    "MAX_DERIVED_COMMUNITY_CACHE",
    "PATH_MAX_ROWS",
    "SUMMARY_LISTING_LIMIT",
    "active_evidence_graph",
    "aggregate_louvain_graph",
    "dead_candidate_row",
    "dead_code_counts",
    "dependency_reach",
    "dependency_seed_nodes",
    "detect_communities",
    "detect_language",
    "enrich_python_semantics",
    "enriched_call",
    "find_argument_flows",
    "find_callees",
    "find_callers",
    "find_dead_code",
    "find_dependencies",
    "find_paths",
    "find_service_paths",
    "flow_cut_report",
    "generation_catalog",
    "get_architecture",
    "hotspot_fields",
    "index_directory",
    "listing_fields",
    "live_source_files",
    "louvain_communities",
    "module_fields",
    "ordered_dead_candidates",
    "parse_file",
    "parse_file_in_workspace",
    "parser_attempt",
    "paths_between",
    "stored_communities",
    "stored_dead_candidates",
    "stored_dependency_rows",
    "stored_modules",
    "summary_listing_limit",
    "tree_sitter_parsers",
    "undirected_call_graph",
]

def _print_unresolved_callers(function_name: str, answer: dict) -> None:
    """A zero that means "could not resolve" has to say so on the CLI too."""
    count = answer.get("unresolved_caller_count", 0)
    if not count:
        return
    print(
        f"  plus {count} call site(s) naming '{function_name}' whose receiver "
        "could not be resolved - candidates, not confirmed callers:"
    )
    for row in answer.get("unresolved_callers", [])[:20]:
        print(f"    {row['file']}:{row['line']}  {row['call_text']} ({row['reason']})")


NO_GENERATION_MESSAGE = (
    "No generation is registered for this repository, so callers cannot be "
    "answered from an index. Build one with get_architecture mode=index "
    "(or `python scripts/repository_index.py index <dir>`), or pass --live to "
    "re-parse every file now, which took 300 s on a 1 026-file repository."
)


def _print_callers(function_name: str, answer: dict) -> None:
    callers = answer["callers"]
    print(f"Callers of '{function_name}': {len(callers)} found.")
    for c in callers[:20]:
        print(f"  {c['file']}:{c['line']}")
    _print_unresolved_callers(function_name, answer)


def _cli_callers(function_name: str, directory: Path, *, live: bool) -> int:
    """Answer from the generation; without one, say so instead of re-parsing.

    Issue #24: `--callers` on a repository without a generation silently fell
    through to the whole-tree parse and timed out at 300 s. An index that
    is missing is a fact worth stating, not a reason to spend five minutes.
    """
    if live:
        _print_callers(function_name, find_callers(function_name, directory, live=True, with_report=True))
        return 0
    stored = stored_callers(function_name, directory, True)
    if stored is None:
        print(NO_GENERATION_MESSAGE)
        return 2
    _print_callers(function_name, stored)
    return 0


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Code graph — tree-sitter code intelligence.")
    p.add_argument(
        "directory", nargs="?", default=".", help="Directory to summarize (read-only)."
    )
    p.add_argument("--callers", type=str, default=None, help="Find callers of a function.")
    p.add_argument(
        "--live",
        action="store_true",
        help="With --callers: re-parse the whole tree instead of reading the generation.",
    )
    args = p.parse_args()

    directory = Path(args.directory)

    if args.callers:
        return _cli_callers(args.callers, directory, live=args.live)

    index_directory(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
