"""Structural architecture answers, their freshness and repository refresh requests."""
from __future__ import annotations

import contextvars
import hashlib
import sys
import threading
import time
from pathlib import Path

from llm_wiki import mcp_server_validation, runtime_roots
from llm_wiki.mcp_server_workers import (
    _bounded_code_graph_call,
    _code_graph_timeout_data,
    bounded_operation_deadline,
    check_deadline,
)


def _dead_code_symbol_view(answer: dict, symbol: str) -> dict:
    """Whether one named function is a dead-code candidate, and why.

    The whole-repository listing cannot answer this question truthfully at this
    repository's scale. Measured 2026-08-29: the analysis produces 846
    candidates, the answer carried 532 of them at 24 982 tokens, and 307 named
    rows were cut to fit the client ceiling. A name missing from a cut list is
    indistinguishable from a name with living callers, so the listing answers
    "is X dead?" with silence that reads as "no". The anchor turns a listing
    that must be trimmed into a verdict that fits whole.

    `not_a_candidate` is the honest phrasing: it says this analysis did not
    nominate the name, which is not the same as proving the name is reachable -
    `graph_complete` in the same answer says how far that claim carries.
    """
    rows = _dead_code_rows_named(answer, symbol)
    kept = _without_key(answer, "candidates")
    return {
        **kept,
        "symbol": symbol,
        "verdict": _dead_code_verdict_word(rows),
        "candidates": rows,
    }


def _dead_code_rows_named(answer: dict, symbol: str) -> list:
    return [row for row in answer.get("candidates", []) if row.get("name") == symbol]


def _without_key(mapping: dict, dropped: str) -> dict:
    return {key: value for key, value in mapping.items() if key != dropped}


def _dead_code_verdict_word(rows: list) -> str:
    if rows:
        return "candidate"
    return "not_a_candidate"


def _narrowed_dead_code(answer: dict, symbol: str | None) -> dict:
    if symbol is None:
        return answer
    return _dead_code_symbol_view(answer, symbol)


def find_dead_code_answer(
    directory: str,
    *,
    symbol: str | None = None,
    live: bool = False,
    deadline: float | None = None,
) -> dict:
    """Find conservative dead-code candidates in a project directory."""
    from llm_wiki.code_graph_dead_code import find_dead_code

    operation_deadline = bounded_operation_deadline(deadline)
    resolved, error = mcp_server_validation.validated_code_directory(
        directory, deadline=operation_deadline
    )
    if error:
        return {"error": error}
    try:
        result = _bounded_code_graph_call(
            find_dead_code,
            resolved,
            live=live,
            with_report=True,
            symbol=symbol,
            deadline=operation_deadline,
        )
    except TimeoutError as reason:
        return _code_graph_timeout_data(
            str(resolved), reason, completed=("directory_validation",)
        )
    return _narrowed_dead_code(_dead_code_result_data(resolved, result), symbol)


def _dead_code_result_data(resolved: Path, result) -> dict:
    if isinstance(result, list):
        return {
            "directory": str(resolved),
            "candidates": result,
            "source_generation": None,
            "graph_complete": False,
            "unresolved_count": None,
            "fallback": True,
        }
    return {"directory": str(resolved), **result}


def architecture_answer(
    directory: str,
    *,
    live: bool = False,
    deadline: float | None = None,
    limit: int | None = None,
) -> dict:
    """Summarize the statically visible architecture of a project directory."""
    from llm_wiki.code_graph_architecture import get_architecture

    operation_deadline = bounded_operation_deadline(deadline)
    resolved, error = mcp_server_validation.validated_code_directory(
        directory, deadline=operation_deadline
    )
    if error:
        return {"error": error}
    try:
        architecture = _bounded_code_graph_call(
            get_architecture,
            resolved,
            live=live,
            with_report=True,
            limit=limit,
            deadline=operation_deadline,
        )
    except TimeoutError as reason:
        return _code_graph_timeout_data(
            str(resolved), reason, completed=("directory_validation",)
        )
    return with_generation_freshness(resolved, _architecture_summary_data(resolved, architecture))


def with_generation_freshness(resolved: Path, answer):
    """The generation's freshness block on an answer that read one, and its refresh.

    The summary and six other modes never compared commits nor started a
    refresh (audit B-35,
    docs/research/2026-09-25-every-structural-answer-carries-its-freshness.md).
    """
    if not _wants_freshness(answer):
        return answer
    freshness = repository_freshness(resolved)
    if freshness is None:
        return answer
    return {**answer, "freshness": freshness}


def _wants_freshness(answer) -> bool:
    if not isinstance(answer, dict):
        return False
    return "error" not in answer and "freshness" not in answer


# What the summary says instead of naming 300 community members. The counts
# stay; only the member dump goes, and the caller is told which mode serves it.
#
# Measured 2026-08-29: `communities` was 13 313 of the summary's 20 076
# architecture tokens - 66.3% - and every one of those bytes is exactly what
# `mode=community` already returns, so two of the thirteen parity tasks were
# paying for the same payload. Worse, `code_graph._communities_holding` records
# that an unanchored listing cannot answer the question callers actually ask of
# it: `fuse_rrf`'s community is 729th by size, so no honest bound reaches it.
# The summary was carrying the expensive half of an answer that could not be
# given without an anchor.
COMMUNITY_MEMBERS_HINT = (
    "omitted from the summary; get_architecture mode=community lists them, and "
    "mode=community with symbol=<name> answers which community a symbol is in"
)


def _summary_architecture(architecture: dict) -> dict:
    """The summary keeps the community counts and drops the member listing."""
    if "communities" not in architecture:
        return architecture
    kept = {key: value for key, value in architecture.items() if key != "communities"}
    return {**kept, "communities": COMMUNITY_MEMBERS_HINT}


def _architecture_summary_data(resolved: Path, architecture: dict) -> dict:
    report = {
        "source_generation": architecture.get("source_generation"),
        "graph_complete": architecture.get("graph_complete", False),
        "unresolved_count": architecture.get("unresolved_count"),
        "fallback": architecture.get("fallback", True),
    }
    return {
        "directory": str(resolved),
        "mode": "summary",
        "architecture": _summary_architecture(architecture),
        **report,
    }


_ARCHITECTURE_SYMBOL_MODES = {"symbol", "callers", "callees", "dependencies"}

ARCHITECTURE_REPORT_KEYS = (
    "source_generation",
    "graph_complete",
    "unresolved_count",
    "fallback",
)

# How far the dependency walk went and whether anything sits past that edge.
# Without these a bounded answer is indistinguishable from a complete one,
# which is the only thing that made the default unsafe to change. Kept apart
# from the rest because only the walk that has a reach may report one.
_ARCHITECTURE_REACH_KEYS = ("depth_applied", "depth_frontier_open")

ARCHITECTURE_REPORT_KEYS = ARCHITECTURE_REPORT_KEYS + _ARCHITECTURE_REACH_KEYS


def _architecture_path_mode_error(mode: str, symbol, target) -> str | None:
    if mode != "path":
        return None
    if symbol and target:
        return None
    return "symbol and target are required for path mode"


def _architecture_argument_error(mode: str, symbol, target) -> str | None:
    if mode in _ARCHITECTURE_SYMBOL_MODES and not symbol:
        return f"symbol is required for {mode} mode"
    return _architecture_path_mode_error(mode, symbol, target)


def architecture_callers(request: dict):
    """Proved static callers, and separately what a trace observed (CODE-09).

    `trace_callers` is a distinct field and never merged into `callers`: a
    trace proves that this call happened in one run, which is a different
    claim from a resolved static edge, and an edge must say how it was
    learned. Measured 2026-08-28 on this repository — 578 of the 757 edges a
    single trace adds land on a method, against 2,477 static ones.
    """
    from llm_wiki.code_graph_calls import find_callers
    from trace_ingest import with_trace_callers

    answer = find_callers(
        request["symbol"],
        request["resolved"],
        live=request["live"],
        with_report=True,
        max_depth=request.get("depth"),
    )
    traced = with_trace_callers(answer, request["symbol"], request["resolved"])
    return _with_lines_from_disk(traced, request)


def _architecture_callees(request: dict):
    from llm_wiki.code_graph_calls import find_callees

    return _with_lines_from_disk(
        find_callees(
            request["symbol"],
            request["resolved"],
            live=request["live"],
            with_report=True,
            max_depth=request.get("depth"),
        ),
        request,
    )


def architecture_dependencies(request: dict):
    """Bounded dependency walk; `depth` names how far, and it pays.

    Measured 2026-08-29 for `scripts/retrieval.py` against the active
    generation: depth 1 answers with 7 modules for ~656 tokens, depth 2
    with 24 rows for ~2 475, and the whole reachable set with 346 rows
    for ~45 817 raw — 20 735 after shaping. The question "which modules
    does this file depend on" is the depth-1 one, and it was costing the
    transitive closure because there was no way to ask for less.

    Additive on purpose: omitting `depth` walks exactly as far as it
    always has.
    """
    from llm_wiki.code_graph_dependencies import find_dependencies

    return find_dependencies(
        request["symbol"],
        request["resolved"],
        reverse=request["reverse"],
        live=request["live"],
        with_report=True,
        max_depth=request.get("depth"),
    )


def _architecture_path(request: dict):
    from llm_wiki.code_graph_dependencies import find_paths

    return find_paths(
        request["symbol"],
        request["target"],
        request["resolved"],
        live=request["live"],
        with_report=True,
    )


def architecture_community(request: dict):
    from llm_wiki.code_graph_communities import detect_communities

    return detect_communities(
        request["resolved"],
        symbol=request.get("symbol"),
        live=request["live"],
        with_report=True,
    )


def _architecture_symbol_dependencies(request: dict):
    """The symbol view never reverses; only the dependencies mode takes that."""
    from llm_wiki.code_graph_dependencies import find_dependencies

    return find_dependencies(
        request["symbol"], request["resolved"], live=request["live"], with_report=True
    )


# The row keys whose lines a file can contradict. A summary's counts, a
# community's roster and a path's hops carry no line of their own.
#
# Audit 3, A3: only a row whose line is the *definition* of the symbol it names
# can be corrected from that symbol's definition. A one-hop caller row, a live
# one and an unresolved one carry the call site beside the caller's name, so
# they are left alone; the depth walk builds its rows from node locations and
# says so with `depth_applied`. Research:
# `docs/research/2026-09-17-a-fresh-line-belongs-to-the-symbol-it-names.md`.
_DEFINITION_ROW_KEYS = ("definition",)
_WALKED_ROW_KEYS = ("callers", "callees", "definition")


def _definition_row_keys(answer) -> tuple[str, ...]:
    if isinstance(answer, dict) and "depth_applied" in answer:
        return _WALKED_ROW_KEYS
    return _DEFINITION_ROW_KEYS


def _with_lines_from_disk(answer, request: dict):
    """Correct any line the file itself has moved on from.

    The generation is rebuilt nightly, so a definition edited today answers with
    yesterday's line until then. One parse of each file the answer names is
    cheaper than being wrong, and nothing is written. Research:
    `docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.
    """
    from fresh_positions import refreshed_answer, refreshed_rows

    if isinstance(answer, list):
        return refreshed_rows(answer, request["resolved"])
    return refreshed_answer(answer, request["resolved"], _definition_row_keys(answer))


def architecture_definition(request: dict) -> dict:
    """Where the symbol is defined, from the generation's definition occurrence.

    `definition_omitted` appears only when the list was cut, like the other keys
    this answer carries only when the source has them.
    """
    from symbol_snippet import definition_report

    report = definition_report(request["resolved"], request["symbol"], request["deadline"])
    answer = {"definition": _with_lines_from_disk(report["sites"], request)}
    omitted = {"definition_omitted": report["sites_omitted"]} if report["sites_omitted"] else {}
    return {**answer, **omitted}


def _architecture_symbol(request: dict) -> dict:
    deadline = request["deadline"]
    callers = architecture_callers(request)
    check_deadline(deadline)
    callees = _architecture_callees(request)
    check_deadline(deadline)
    dependencies = _architecture_symbol_dependencies(request)
    check_deadline(deadline)
    return {
        "symbol": request["symbol"],
        # A "where is it" question is answered by the definition, not by the
        # call sites around it (parity run 2026-09-12, task T04).
        **architecture_definition(request),
        "callers": callers.get("callers", []),
        "callees": callees.get("callees", []),
        "dependencies": dependencies.get("dependencies", []),
        # Only keys the source actually carries. Copying the whole list
        # unconditionally spelled every absent one as an explicit `null`,
        # which costs tokens to say nothing and reads as a measured value.
        **_present_keys(callers, ARCHITECTURE_REPORT_KEYS),
        # The reach belongs to the walk that has one, and that is the
        # dependency walk, not the caller lookup beside it.
        **_present_keys(dependencies, _ARCHITECTURE_REACH_KEYS),
    }


def _present_keys(source: dict, keys) -> dict:
    return {key: source[key] for key in keys if key in source}


_ARCHITECTURE_MODE_QUERIES = {
    "callers": architecture_callers,
    "callees": _architecture_callees,
    "dependencies": architecture_dependencies,
    "path": _architecture_path,
    "community": architecture_community,
}


def _architecture_report(architecture) -> dict:
    if not isinstance(architecture, dict):
        return {}
    return {
        key: architecture[key]
        for key in ARCHITECTURE_REPORT_KEYS
        if key in architecture
    }


# Issue #24, section A: a structural answer names the commit its generation
# was built from against the commit the checkout is at, and when they differ
# the bounded incremental refresh is started detached - once per repository
# and commit in this process - so the session never waits for it. See
# `docs/research/2026-09-10-warm-index-answers-inside-the-loop.md`.
REFRESH_REQUESTED: dict[str, str] = {}
_REFRESH_REQUESTED_LOCK = threading.Lock()


def _freshness_fields(resolved: Path, architecture) -> dict:
    """Only answers read from a generation have a commit to compare; an answer
    without one may be a new worktree of a registered repository (#24, D1)."""
    if _architecture_report(architecture).get("source_generation") is None:
        return worktree_follow_fields(resolved)
    freshness = repository_freshness(resolved)
    return {} if freshness is None else {"freshness": freshness}


# Issue #24, section D1: a worktree of a registered repository gets its own
# generation without anyone running the indexer. The first structural answer
# that finds none starts the fenced `follow` detached, once per checkout in
# this process, and says so; the nightly `refresh-all` covers the rest.
FOLLOW_REQUESTED: set[str] = set()


def _unindexed_worktree(resolved: Path) -> Path | None:
    import subprocess

    from repository_worktrees import unindexed_worktree_root

    try:
        return unindexed_worktree_root(resolved, deadline=time.monotonic() + 5.0)
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return None


def worktree_follow_fields(resolved: Path) -> dict:
    if _is_the_vault(resolved):
        return {}
    checkout = _unindexed_worktree(resolved)
    if checkout is None:
        return {}
    return {"freshness": {"generation_commit": None, "refresh": request_worktree_follow(checkout)}}


def _follow_log_paths(checkout: Path) -> tuple[Path, Path]:

    folder = Path(runtime_roots.state_root()) / "logs" / "repository-refresh"
    stem = "follow-" + hashlib.sha256(str(checkout).encode("utf-8")).hexdigest()[:16]
    return folder / f"{stem}.out.log", folder / f"{stem}.err.log"


def request_worktree_follow(checkout: Path) -> str:
    """Start the fenced worktree index once per checkout; never wait for it."""
    from memory_state import spawn_detached

    with _REFRESH_REQUESTED_LOCK:
        if str(checkout) in FOLLOW_REQUESTED:
            return "worktree_follow_already_requested"
        FOLLOW_REQUESTED.add(str(checkout))
    out_log, err_log = _follow_log_paths(checkout)
    script = Path(__file__).resolve().parents[1] / "repository_index.py"
    pid = spawn_detached(
        [sys.executable, str(script), "follow", str(checkout)],
        stdout_path=out_log,
        stderr_path=err_log,
    )
    if pid is None:
        _forget_follow_request(checkout)
        return "spawn_failed"
    return "worktree_follow_started"


def _forget_follow_request(checkout: Path) -> None:
    """A spawn that failed was not a request: the next question tries again.

    Audit 3, B2. Research:
    `docs/research/2026-09-17-a-refresh-that-never-started-is-asked-for-again.md`.
    """
    with _REFRESH_REQUESTED_LOCK:
        FOLLOW_REQUESTED.discard(str(checkout))


def _forget_refresh_request(checkout) -> None:
    """Take back this commit's mark, and only this commit's."""
    with _REFRESH_REQUESTED_LOCK:
        if REFRESH_REQUESTED.get(checkout.checkout_id) == checkout.git_commit:
            del REFRESH_REQUESTED[checkout.checkout_id]


# The freshness block decorates an answer that is already computed, so a
# generation that cannot be read must say so rather than turn a good structural
# answer into an error at the tool boundary (audit 3, B26), and the read is
# bounded in time like every other one on this path (audit 3, B3). Research:
# `docs/research/2026-09-18-graph-an-unreadable-generation-must-not-fail-a-good-answer.md`.
FRESHNESS_LIMIT_SECONDS = 5.0


def repository_freshness(resolved: Path) -> dict | None:
    from llm_wiki import code_graph_generation

    try:
        lease = code_graph_generation.active_evidence_graph(
            resolved, deadline=time.monotonic() + FRESHNESS_LIMIT_SECONDS
        )
    except code_graph_generation.GenerationUnreadable as error:
        return {"unavailable": error.reason}
    except TimeoutError:
        return {"unavailable": "deadline"}
    if lease is None:
        return None
    return _freshness_of_lease(resolved, lease)


def _freshness_of_lease(resolved: Path, lease) -> dict | None:
    try:
        checkout = getattr(lease, "cached_scope", None)
        if checkout is None:
            return None
        return _freshness_block(
            resolved, checkout, lease.repository_scope, lease.generation_id
        )
    finally:
        lease.close()


def _confirmed_at_commit(checkout, generation_id) -> bool:
    """Whether a refresh already confirmed this generation against this commit.

    A commit that changes no source leaves the generation's own commit behind
    for ever, and the checkout would be called stale until something edited a
    file. The hint table records the commit its generation was last confirmed
    at. See `docs/research/2026-09-17-small-corrections-in-the-generations-area.md`.
    """
    import sqlite3

    from code_hints import hints_path, read_meta

    try:
        meta = read_meta(hints_path(Path(runtime_roots.state_root()), checkout.checkout_id))
    except (OSError, ValueError, sqlite3.Error):
        return False
    if meta is None:
        return False
    return (meta.get("generation_id"), meta.get("git_commit")) == (
        str(generation_id),
        str(checkout.git_commit or ""),
    )


def _freshness_block(resolved: Path, checkout, generation, generation_id) -> dict:
    stale = checkout.git_commit != generation.git_commit and not _confirmed_at_commit(
        checkout, generation_id
    )
    return {
        "generation_commit": generation.git_commit,
        "checkout_commit": checkout.git_commit,
        "stale_by_commit": stale,
        "refresh": refresh_action(checkout, stale),
    }


def _is_the_vault(resolved: Path) -> bool:

    return resolved in {Path(runtime_roots.vault_root()).resolve(), Path(runtime_roots.state_root()).resolve()}


def refresh_action(checkout, stale: bool) -> str:
    """What a stale answer does about it, judged on the checkout it belongs to.

    Judging the directory asked about took `<vault>/scripts` for a foreign
    checkout and refreshed the vault in the daytime (audit 2026-09-26 B-10,
    docs/research/2026-09-26-the-vault-check-reads-the-checkout-root.md).
    """
    if not stale:
        return "not_needed"
    if _is_the_vault(Path(checkout.checkout_root).resolve()):
        # The vault's own memory generation is rebuilt and activated by the
        # nightly pass. Its code generation is refreshed
        # by the same nightly step as every other checkout's since 2026-09-12;
        # this path does not start one here (audit 3, G-L5).
        return "vault_nightly"
    return request_repository_refresh(checkout)


def _refresh_log_paths(identity: str) -> tuple[Path, Path]:

    folder = Path(runtime_roots.state_root()) / "logs" / "repository-refresh"
    stem = identity.rsplit(":", 1)[-1][:16]
    return folder / f"{stem}.out.log", folder / f"{stem}.err.log"


def request_repository_refresh(checkout) -> str:
    """Start the bounded refresh once per (checkout, commit); never wait for it.

    It ran on the directory asked about, which a subfolder's refresh refused
    while the answer said `started`, and one worktree's request stood for every
    worktree of the repository (audit B-38,
    docs/research/2026-09-25-a-refresh-runs-on-its-checkout.md).
    """
    from memory_state import spawn_detached

    with _REFRESH_REQUESTED_LOCK:
        if REFRESH_REQUESTED.get(checkout.checkout_id) == checkout.git_commit:
            return "already_requested"
        REFRESH_REQUESTED[checkout.checkout_id] = checkout.git_commit
    out_log, err_log = _refresh_log_paths(checkout.checkout_id)
    script = Path(__file__).resolve().parents[1] / "repository_index.py"
    pid = spawn_detached(
        [sys.executable, str(script), "refresh", str(Path(checkout.checkout_root))],
        stdout_path=out_log,
        stderr_path=err_log,
    )
    if pid is None:
        _forget_refresh_request(checkout)
        return "spawn_failed"
    return "started"


def get_architecture_mode(
    directory: str,
    *,
    mode: str,
    symbol: str | None = None,
    target: str | None = None,
    reverse: bool = False,
    depth: int | None = None,
    live: bool = False,
    deadline: float | None = None,
) -> dict:
    """Dispatch bounded graph queries while retaining live/store reports."""
    resolved, error = mcp_server_validation.validated_code_directory(directory, deadline=deadline)
    if error:
        return {"error": error}
    argument_error = _architecture_argument_error(mode, symbol, target)
    if argument_error is not None:
        return {"error": argument_error}
    check_deadline(deadline)
    operation_deadline = bounded_operation_deadline(deadline)
    query = _ARCHITECTURE_MODE_QUERIES.get(mode, _architecture_symbol)
    request = {
        "resolved": resolved,
        "symbol": symbol,
        "target": target,
        "reverse": reverse,
        "depth": depth,
        "live": live,
        "deadline": operation_deadline,
    }
    try:
        architecture = _bounded_mode_query(query, request, operation_deadline)
    except TimeoutError as reason:
        return _code_graph_timeout_data(str(resolved), reason, completed=("directory_validation",))
    return {
        "directory": str(resolved),
        "mode": mode,
        "architecture": architecture,
        **_architecture_report(architecture),
        **_freshness_fields(resolved, architecture),
    }


def _bounded_mode_query(query, request: dict, deadline: float):
    """Every mode on the bounded code-graph workers, as the summary always was.

    The modes parsed the tree on the tool's own thread with no deadline; four
    hung calls held every MCP slot (audit B-33,
    docs/research/2026-09-25-every-architecture-mode-is-bounded.md).
    """
    return _bounded_code_graph_call(
        contextvars.copy_context().run, query, request, deadline=deadline
    )


def analyze_change_impact(
    *,
    directory: str,
    comparison: str = "dirty",
    base: str | None = None,
    target: str | None = None,
    branch: str | None = None,
    deadline: float | None = None,
) -> dict:
    """Run bounded diff-to-graph impact through the architecture tool."""
    from impact_analysis import COMPARISONS, analyze_impact

    resolved, error = mcp_server_validation.validated_code_directory(directory, deadline=deadline)
    if error:
        return {"error": error}
    if comparison not in COMPARISONS:
        return {"error": "invalid impact comparison"}
    return analyze_impact(
        root=resolved,
        comparison=comparison,
        base=base,
        target=target,
        branch=branch,
        deadline=deadline,
    )
