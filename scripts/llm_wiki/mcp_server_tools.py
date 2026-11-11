"""Tool handlers, dispatch and the answer of one tool call."""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from answer_budget import shape_code_answer
from answer_cost import attach_answer_cost
from llm_wiki import (
    mcp_server_architecture,
    mcp_server_doctor,
    mcp_server_freshness,
    mcp_server_navigation,
    mcp_server_quality,
    mcp_server_sanitize,
    mcp_server_validation,
    mcp_server_vault,
    mcp_server_workers,
    runtime_roots,
)
from llm_wiki.mcp_server_architecture import (
    get_architecture_mode,
    with_generation_freshness,
)
from llm_wiki.mcp_server_freshness import (
    index_timestamp,
)
from llm_wiki.mcp_server_limits import _OPERATION_DEADLINE
from llm_wiki.mcp_server_navigation import (
    normalized_navigation_failure,
)
from llm_wiki.mcp_server_sanitize import (
    _clamped_limit,
    _is_rendered_navigation_data,
    _safe_exception_text,
)
from llm_wiki.mcp_server_schemas import TOOL_INPUT_SCHEMAS
from llm_wiki.mcp_server_vault import (
    agent_row,
    retrieval_trace,
)
from llm_wiki.mcp_server_workers import (
    _busy_envelope_text,
    _call_with_deadline,
    _code_graph_timeout_data,
    _WorkerCapacityExhausted,
    check_deadline,
    is_precise_architecture_request,
    rendered_envelope,
    timeout_envelope_text,
    tool_operation_seconds,
)


def _navigation_failure_from_arguments(
    arguments: object,
    *,
    status: str,
    warning: str,
) -> dict | None:
    if not isinstance(arguments, dict) or not is_precise_architecture_request(
        arguments
    ):
        return None
    mode = arguments.get("mode")
    if not isinstance(mode, str):
        return None
    directory = arguments.get("directory")
    return normalized_navigation_failure(
        directory=directory if isinstance(directory, str) else None,
        mode=mode,
        status=status,
        warning=warning,
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 10),
    )


def tool_timeout_envelope_text(name: str, arguments: object) -> str:

    data = (
        _navigation_failure_from_arguments(
            arguments,
            status="timeout",
            warning="navigation_timeout",
        )
        if name == "get_architecture"
        else None
    )
    if data is None:
        return timeout_envelope_text()
    data["directory"] = None
    warning = "navigation_timeout"
    envelope = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "index_timestamp": None,
        "source_commit": None,
        "freshness": "unknown",
        "coverage": 0.0,
        "confidence": 0.2,
        "fallback": False,
        "partial": True,
        "warnings": [warning],
        "components": {},
        "data": data,
    }
    return rendered_envelope(envelope)


def _grounded_recall(arguments: dict, deadline: float):
    from query_memory import grounded_qa

    return grounded_qa(
        arguments["query"],
        vault=runtime_roots.vault_root(),
        profile=arguments.get("profile"),
        deadline=deadline,
    )


def _tool_recall(arguments: dict, deadline: float):
    if arguments.get("grounded", False):
        return _grounded_recall(arguments, deadline), False
    effective_limit, limit_clamped = _clamped_limit(arguments.get("limit", 8))
    reported: dict[str, object] = {}
    results = _call_with_deadline(
        mcp_server_vault.search_vault,
        arguments["query"],
        limit=effective_limit,
        deadline=deadline,
        trace_sink=reported,
    )
    trace = retrieval_trace(arguments["query"], results, reported)
    data = {
        "results": [agent_row(row) for row in results],
        "retrieval_trace": trace,
        "_meta": _call_with_deadline(mcp_server_validation.meta, deadline=deadline),
    }
    return data, limit_clamped


def _tool_read_page(arguments: dict, deadline: float):
    return _call_with_deadline(mcp_server_vault.read_page, arguments["slug"], deadline=deadline), False


def _tool_wiki_overview(arguments: dict, deadline: float):
    data = _call_with_deadline(mcp_server_vault.wiki_overview, deadline=deadline)
    data["_meta"] = _call_with_deadline(mcp_server_validation.meta, deadline=deadline)
    return data, False


def _tool_vault_status(arguments: dict, deadline: float):
    return _call_with_deadline(mcp_server_vault.vault_status, deadline=deadline), False


def tool_get_decisions(arguments: dict, deadline: float):
    """Decision pages, with the same trace and freshness a recall carries."""
    effective_limit, limit_clamped = _clamped_limit(arguments.get("limit", 10))
    reported: dict[str, object] = {}
    query = arguments.get("query")
    results = _call_with_deadline(
        mcp_server_vault.get_decisions, query, limit=effective_limit, deadline=deadline, trace_sink=reported
    )
    data = {
        "results": results,
        "retrieval_trace": retrieval_trace(query or "decision", results, reported),
        "_meta": _call_with_deadline(mcp_server_validation.meta, deadline=deadline),
    }
    return data, limit_clamped


def _context_token_budget_options(arguments: dict) -> dict:
    if "token_budget" in arguments:
        return {"token_budget": arguments["token_budget"]}
    return {}


def _tool_get_context(arguments: dict, deadline: float):
    data = _call_with_deadline(
        mcp_server_vault.get_context,
        arguments["slugs"],
        arguments.get("include"),
        **_context_token_budget_options(arguments),
        deadline=deadline,
    )
    return data, False


def _tool_check_contradiction(arguments: dict, deadline: float):
    data = _call_with_deadline(
        mcp_server_vault.check_contradiction, arguments["claim"], deadline=deadline
    )
    return data, False


def _tool_log_decision(arguments: dict, deadline: float):
    data = _call_with_deadline(
        mcp_server_vault.log_decision,
        arguments["summary"],
        arguments.get("rationale", ""),
        deadline=deadline,
    )
    return data, False


def _tool_compile(arguments: dict, deadline: float):
    return _call_with_deadline(mcp_server_vault.trigger_compile, deadline=deadline), False


def shaped_code_answer(data, arguments: dict):
    """CODE-06: one place where every code answer is measured and reduced.

    Applied here rather than inside `code_graph` or `graph_query` because this
    is the only layer that sees every mode's answer in one shape - and because
    those two modules belong to another agent's task in this session.
    """
    return shape_code_answer(
        data,
        budget_tokens=arguments.get("budget_tokens"),
        include_node_ids=bool(arguments.get("include_node_ids", False)),
    )


def _tool_find_dead_code(arguments: dict, deadline: float):
    try:
        data = _call_with_deadline(
            mcp_server_architecture.find_dead_code_answer,
            arguments.get("directory"),
            symbol=arguments.get("symbol"),
            live=arguments.get("live", False),
            deadline=deadline,
        )
    except TimeoutError as error:
        return _code_graph_timeout_data(
            arguments.get("directory"), error, completed=()
        ), False
    return shaped_code_answer(data, arguments), False


def _precise_architecture_call(arguments: dict, deadline: float):
    missing = [
        key
        for key in ("directory", "path", "line", "character")
        if key not in arguments
    ]
    if missing:
        return {"error": f"missing required arguments: {', '.join(missing)}"}
    return mcp_server_navigation.get_precise_architecture(
        arguments["directory"],
        mode=arguments.get("mode"),
        path=arguments["path"],
        line=arguments["line"],
        character=arguments["character"],
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 10),
        deadline=deadline,
    )


def _impact_architecture_call(arguments: dict, deadline: float):
    """Diff to graph, plus the code symbols the diff reaches (issue #24, B5)."""
    from impact_symbols import affected_symbols

    impact = mcp_server_architecture.analyze_change_impact(
        directory=arguments.get("directory"),
        comparison=arguments.get("comparison", "dirty"),
        base=arguments.get("base"),
        target=arguments.get("target"),
        branch=arguments.get("branch"),
        deadline=deadline,
    )
    if "error" in impact:
        return impact
    directory = Path(arguments["directory"]).resolve()
    return {
        **impact,
        **affected_symbols(directory, impact.get("changed_symbols", []), deadline),
    }


def _summary_architecture_call(arguments: dict, deadline: float):
    return _call_with_deadline(
        mcp_server_architecture.architecture_answer,
        arguments.get("directory"),
        live=arguments.get("live", False),
        limit=arguments.get("limit"),
        deadline=deadline,
    )


def _architecture_mode_call(arguments: dict, deadline: float):
    return get_architecture_mode(
        arguments.get("directory"),
        mode=arguments["mode"],
        symbol=arguments.get("symbol"),
        target=arguments.get("target"),
        reverse=arguments.get("reverse", False),
        depth=arguments.get("depth"),
        live=arguments.get("live", False),
        deadline=deadline,
    )


def _provenance_architecture_call(arguments: dict, deadline: float):
    """MEM-16: symbol -> decision pages naming it -> their cited sources."""
    from provenance_join import join_symbol_provenance

    directory = Path(arguments["directory"]).resolve()
    return join_symbol_provenance(
        Path(runtime_roots.vault_root()), directory, str(arguments["symbol"]), deadline
    )


def _snippet_architecture_call(arguments: dict, deadline: float):
    """CODE-02: bounded source blocks for a symbol, via the active generation."""
    from symbol_snippet import snippet_for_symbol

    directory = Path(arguments["directory"]).resolve()
    return snippet_for_symbol(directory, str(arguments["symbol"]), deadline)


def _coverage_architecture_call(arguments: dict, deadline: float):
    """CODE-05: is this path indexed, fresh, and how many nodes — honestly."""
    from path_coverage import coverage_for_path

    directory = Path(arguments["directory"]).resolve()
    return coverage_for_path(directory, str(arguments["path"]), deadline)


def _search_architecture_call(arguments: dict, deadline: float):
    """Issue #24, B2: ranked qualified names with degree, whole generation."""
    from symbol_search import search_symbols

    directory = Path(arguments["directory"]).resolve()
    path_prefix = arguments.get("path")
    return search_symbols(
        directory,
        str(arguments["symbol"]),
        path_prefix=None if path_prefix is None else str(path_prefix),
        limit=arguments.get("limit"),
        deadline=deadline,
    )


def data_flow_architecture_call(arguments: dict, deadline: float):
    """Issue #24, B3: which argument binds which parameter, hop by hop.

    The walk carries the caller's deadline and cancellation, so it stops when
    the caller has stopped waiting instead of holding one of the four worker
    slots to the end (audit 3, B3).
    """
    from llm_wiki.code_graph_flows import find_argument_flows

    directory = Path(arguments["directory"]).resolve()
    answer = find_argument_flows(
        str(arguments["symbol"]),
        directory,
        max_depth=arguments.get("depth"),
        deadline=deadline,
        cancelled=mcp_server_workers.operation_cancelled(),
    )
    if answer is None:
        return {"flows": [], "mode": "index", "reason": "no_active_generation"}
    return answer


def cross_service_architecture_call(arguments: dict, deadline: float):
    """Issue #24, B3: follow a request across the route it reaches.

    Bounded by the caller's deadline and cancellation, like `data_flow`.
    """
    from llm_wiki.code_graph_flows import find_service_paths

    directory = Path(arguments["directory"]).resolve()
    answer = find_service_paths(
        str(arguments["symbol"]),
        directory,
        max_depth=arguments.get("depth"),
        deadline=deadline,
        cancelled=mcp_server_workers.operation_cancelled(),
    )
    if answer is None:
        return {"hops": [], "mode": "index", "reason": "no_active_generation"}
    return answer


def _query_architecture_call(arguments: dict, deadline: float):
    """CODE-01: bounded multi-hop JSON pipeline over the active generation."""
    from graph_query import run_graph_query

    directory = Path(arguments["directory"]).resolve()
    return run_graph_query(directory, str(arguments["query"]), deadline)


def _repository_index_call(call, arguments: dict, deadline: float):
    """Turn a named CODE-03 refusal into an answer instead of an exception."""
    from repository_indexing import (
        RepositoryIndexRefused,
    )

    try:
        return call(arguments, deadline)
    except RepositoryIndexRefused as refusal:
        return refusal.as_dict()


def _index_architecture_call(arguments: dict, deadline: float):
    """CODE-03: build and register a generation for another local repository."""
    return _repository_index_call(_index_repository_now, arguments, deadline)


def _index_repository_now(arguments: dict, deadline: float):
    from repository_indexing import (
        index_repository,
    )

    return index_repository(
        arguments["directory"],
        roots=arguments.get("roots"),
        deadline=deadline,
        cancelled=mcp_server_workers.operation_cancelled(),
    )


def _repositories_architecture_call(arguments: dict, deadline: float):
    """CODE-03: which repositories have a registered generation, and what it covers."""
    return _repository_index_call(_list_repositories_now, arguments, deadline)


def _list_repositories_now(_arguments: dict, deadline: float):
    from repository_indexing import (
        list_repositories,
    )

    return list_repositories(deadline=deadline)


def _changes_architecture_call(arguments: dict, deadline: float):
    """CODE-03: what changed in a repository since its newest generation."""
    return _repository_index_call(_detect_changes_now, arguments, deadline)


def _detect_changes_now(arguments: dict, deadline: float):
    from repository_indexing import (
        detect_repository_changes,
    )

    return detect_repository_changes(
        arguments["directory"],
        deadline=deadline,
        cancelled=mcp_server_workers.operation_cancelled(),
    )


def tool_get_architecture(arguments: dict, deadline: float):
    try:
        data = architecture_tool_call(arguments, deadline)
        return shaped_code_answer(data, arguments), False
    except TimeoutError as error:
        return _architecture_timeout_data(arguments, error), False
    except ValueError as error:
        # Measured 2026-08-28: symbol mode on a common name raised a bare
        # "query row ceiling exceeded" through the dispatcher. A tool answers
        # with a named refusal, never an exception.
        return {
            "status": "error",
            "mode": arguments.get("mode", "summary"),
            "error": str(error)[:200],
        }, False


def architecture_tool_call(arguments: dict, deadline: float):
    if is_precise_architecture_request(arguments):
        return _precise_architecture_call(arguments, deadline)
    mode = arguments.get("mode", "summary")
    calls = {
        "impact": _impact_architecture_call,
        "summary": _summary_architecture_call,
        "provenance": _provenance_architecture_call,
        "snippet": _snippet_architecture_call,
        "coverage": _coverage_architecture_call,
        "search": _search_architecture_call,
        "query": _query_architecture_call,
        "data_flow": data_flow_architecture_call,
        "cross_service": cross_service_architecture_call,
        "index": _index_architecture_call,
        "repositories": _repositories_architecture_call,
        "changes": _changes_architecture_call,
    }
    call = calls.get(mode, _architecture_mode_call)
    if mode in DIRECTORY_CHECKED_MODES:
        return _checked_directory_call(call, arguments, deadline)
    return call(arguments, deadline)


# The task-shaped modes that answer about `directory` and used to only
# `resolve()` it: a relative path meant the server's working directory and a
# filesystem root was accepted (audit 3, B1). Research:
# `docs/research/2026-09-17-every-mode-checks-the-directory-it-is-given.md`.
DIRECTORY_CHECKED_MODES = frozenset(
    {"provenance", "snippet", "coverage", "search", "query", "data_flow", "cross_service"}
)


def _checked_directory_call(call, arguments: dict, deadline: float):
    """Run one mode on a directory that passed the validator the older modes use."""
    resolved, error = mcp_server_validation.validated_code_directory(arguments.get("directory"), deadline=deadline)
    if error:
        return {"error": error}
    return with_generation_freshness(resolved, call({**arguments, "directory": str(resolved)}, deadline))


def _architecture_timeout_data(arguments: dict, error: BaseException) -> dict:
    """Precise requests keep their navigation-failure shape; the rest name the budget."""
    failure = _navigation_failure_from_arguments(
        arguments,
        status=_navigation_error_status(error),
        warning=_navigation_error_warning(error),
    )
    if failure is not None:
        return failure
    return _code_graph_timeout_data(arguments.get("directory"), error, completed=())


def _tool_doctor(arguments: dict, deadline: float):
    return _call_with_deadline(mcp_server_doctor.doctor, **arguments, deadline=deadline), False


TOOL_HANDLERS = {
    "recall": _tool_recall,
    "read_page": _tool_read_page,
    "wiki_overview": _tool_wiki_overview,
    "vault_status": _tool_vault_status,
    "get_decisions": tool_get_decisions,
    "get_context": _tool_get_context,
    "check_contradiction": _tool_check_contradiction,
    "log_decision": _tool_log_decision,
    "compile": _tool_compile,
    "find_dead_code": _tool_find_dead_code,
    "get_architecture": tool_get_architecture,
}


def dispatch_tool(name: str, arguments, deadline: float):
    handler = TOOL_HANDLERS.get(name, _tool_doctor)
    return handler(arguments, deadline)


def _navigation_error_status(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    return "error"


def _navigation_error_warning(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "navigation_timeout"
    return "navigation_failed"


def _tool_call_failure(name: str, arguments, error: BaseException) -> dict:
    """The failing tool names itself, so the trace can say which one it was."""
    if name != "get_architecture":
        return {"error": _safe_exception_text(error, f"mcp.{name}")}
    failure = _navigation_failure_from_arguments(
        arguments,
        status=_navigation_error_status(error),
        warning=_navigation_error_warning(error),
    )
    if failure is None:
        return {"error": _safe_exception_text(error, f"mcp.{name}")}
    return failure


def tool_call_data(name: str, arguments, operation_deadline: float):
    """Return the tool payload and whether its limit argument was clamped."""
    check_deadline(operation_deadline)
    if name not in TOOL_INPUT_SCHEMAS:
        return {"error": f"Unknown tool: {name}"}, False
    check_deadline(operation_deadline)
    validation_error = mcp_server_validation.validate_tool_arguments(name, arguments)
    check_deadline(operation_deadline)
    if validation_error is not None:
        return {"error": validation_error}, False
    try:
        check_deadline(operation_deadline)
        return dispatch_tool(name, arguments, operation_deadline)
    except Exception as error:  # noqa: BLE001 - stable tool failure boundary
        return _tool_call_failure(name, arguments, error), False


def _dict_arguments(arguments):
    if isinstance(arguments, dict):
        return arguments
    return None


def tool_call_envelope(
    name: str, data, arguments, limit_clamped: bool, operation_deadline: float
) -> dict:
    if _is_rendered_navigation_data(name, data):
        data = mcp_server_sanitize.sanitize_navigation_data(data)
    check_deadline(operation_deadline)
    quality = mcp_server_quality.quality_for(
        name, data, _dict_arguments(arguments), limit_clamped=limit_clamped
    )
    check_deadline(operation_deadline)
    components = mcp_server_freshness.components_for(name, data)
    check_deadline(operation_deadline)
    return mcp_server_quality.build_operation_envelope(
        data,
        quality,
        components=components,
        index_timestamp=index_timestamp(name, data),
        source_root=_answer_source_root(name, arguments),
    )


def _answer_source_root(name: str, arguments) -> str | None:
    """The checkout a directory-scoped tool read, or None for the vault's answers.

    Found from the schema, so a tool that gains a `directory` argument is
    covered without being listed here (audit 2026-09-26 C-9).
    """
    if "directory" not in TOOL_INPUT_SCHEMAS.get(name, {}).get("properties", {}):
        return None
    directory = _dict_arguments(arguments).get("directory")
    return directory if isinstance(directory, str) else ""


def _record_answer_cost(envelope: dict, started: float, operation_deadline: float) -> None:
    """OPS-02: the answer states what it cost, and never fails for saying so.

    Attached here because this is the one funnel every tool call passes, and
    the last point before the answer is serialised - so the estimate covers
    the finished envelope rather than a payload that later grew. A telemetry
    failure leaves the key absent, which by the module's contract reads as
    "not measured" and never as "free".
    """
    try:
        attach_answer_cost(
            envelope,
            elapsed_seconds=time.monotonic() - started,
            budget_seconds=operation_deadline - started,
        )
    except Exception:  # noqa: BLE001 - an answer outranks its own cost line
        return


def execute_tool_call(name: str, arguments, operation_deadline: float) -> str:
    """Execute one tool under the absolute deadline created by its async handler."""

    started = time.monotonic()
    deadline_token = _OPERATION_DEADLINE.set(operation_deadline)
    try:
        data, limit_clamped = tool_call_data(name, arguments, operation_deadline)
        check_deadline(operation_deadline)
        return answer_text(name, arguments, data, limit_clamped, started, operation_deadline)
    finally:
        _OPERATION_DEADLINE.reset(deadline_token)


def answer_text(
    name: str, arguments, data, limit_clamped: bool, started: float, operation_deadline: float
) -> str:
    """The envelope of a finished tool call, behind the same boundary as the tool.

    A failure while building or rendering it used to leave the handler, and the
    SDK sent its raw text, paths included (audit B-20,
    docs/research/2026-09-25-an-envelope-failure-is-a-safe-answer.md).
    """
    try:
        envelope = tool_call_envelope(name, data, arguments, limit_clamped, operation_deadline)
        check_deadline(operation_deadline)
        _record_answer_cost(envelope, started, operation_deadline)
        return rendered_envelope(envelope)
    except TimeoutError:
        raise
    except Exception as error:  # noqa: BLE001 - stable answer boundary
        return rendered_envelope(mcp_server_quality.build_operation_envelope(_tool_call_failure(name, arguments, error)))


async def _bounded_tool_call(name: str, arguments, execute, render):
    """One tool call, off-loop, under its own deadline, with its refusal answers.

    Both entry points share this policy: the registered `call_tool` callback
    renders formatted results, `handle_tool_call` renders text. Audit 3, B5:
    they used to be twins and had already drifted apart.
    """
    operation_deadline = time.monotonic() + tool_operation_seconds(name, arguments)
    try:
        return await mcp_server_workers.run_bounded(
            execute, name, arguments, operation_deadline, deadline=operation_deadline
        )
    except _WorkerCapacityExhausted:
        return render(_busy_envelope_text())
    except TimeoutError:
        return render(tool_timeout_envelope_text(name, arguments))


def _same_text(text: str) -> str:
    return text


async def handle_tool_call(name: str, arguments) -> str:
    """One tool call as envelope text, without blocking unrelated event-loop work."""
    return await _bounded_tool_call(name, arguments, execute_tool_call, _same_text)
