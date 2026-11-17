"""How fresh each component of an answer is."""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
from pathlib import Path

from bounded_io import (
    MAX_KNOWLEDGE_PAGE_BYTES,
    read_stable_bytes,
)
from llm_wiki import runtime_roots
from llm_wiki.mcp_server_limits import RETRIEVAL_TOOLS
from llm_wiki.mcp_server_sanitize import _is_rendered_navigation_data

# Reached only when the freshness already is "stale", so the ceiling was never
# anything but 0.6; the ternary that said otherwise could not choose its second
# branch.
_STALE_COMPONENT_CEILING = 0.6


def _degrade_stale_envelope(envelope: dict) -> None:
    envelope["coverage"] = min(envelope["coverage"], _STALE_COMPONENT_CEILING)
    envelope["confidence"] = min(envelope["confidence"], _STALE_COMPONENT_CEILING)
    envelope["partial"] = True
    warning = "One or more response components are stale."
    if warning not in envelope["warnings"]:
        envelope["warnings"].append(warning)


def _signal_freshness(signal: str, signals: set, answer: str) -> str:
    """The answer's own freshness, for a signal that ran."""
    if signal not in signals:
        return "missing"
    return answer


def _reranker_freshness(trace: dict) -> str:
    if trace.get("reranker_applied"):
        return "fresh"
    if trace.get("reranker_fallback_reason"):
        return "missing"
    return "unknown"


def _generation_manifest(generation: object) -> Path | None:

    if not isinstance(generation, str) or not generation:
        return None
    return runtime_roots.state_root() / "cache" / "evidence-graph" / "generations" / generation / "manifest.json"


def generation_built_ns(generation: object) -> int | None:
    """When the generation an answer came from was written, or None."""
    manifest = _generation_manifest(generation)
    try:
        return None if manifest is None else manifest.stat().st_mtime_ns
    except OSError:
        return None


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def answer_freshness(generation: object, paths: list[str]) -> str:
    """Whether this answer's own sources still say what the generation holds.

    Stale when a source the answer cites changed or vanished since the build, or
    when a compiled note did (knowledge the index cannot see yet). An uncited
    project `state.md`, rewritten by sessions all day, no longer makes every answer
    stale (audit 2026-09-27 B-10,
    docs/research/2026-09-27-an-answer-is-fresh-by-its-own-sources.md).
    """
    built = generation_built_ns(generation)
    if built is None:
        return "fresh"
    return _freshness_from_digests(generation, paths, built)


def _freshness_from_digests(generation: object, paths: list[str], built: int) -> str:
    recorded = recorded_memory_digests(generation)
    if recorded is None:
        return "unknown"
    if _cited_source_moved(recorded, paths, built) or _notes_moved(recorded, built):
        return "stale"
    return "fresh"


def _cited_source_moved(recorded: dict[str, str], paths: list[str], built: int) -> bool:

    cited = [path for path in paths if _is_memory_path(path)]
    return any(_cited_changed(runtime_roots.vault_root(), path, recorded, built) for path in cited)


def _cited_changed(root: Path, relative: str, recorded: dict[str, str], built: int) -> bool:
    path = root / relative
    if not path.is_file():
        return True
    return _changed_since(root, path, recorded, built)


def _notes_moved(recorded: dict[str, str], built: int) -> bool:
    """A compiled note was added, edited or removed since the build."""

    current = list((runtime_roots.vault_root() / "knowledge" / "notes").rglob("*.md"))
    if _recorded_notes(recorded) - {path.relative_to(runtime_roots.vault_root()).as_posix() for path in current}:
        return True
    return any(_changed_since(runtime_roots.vault_root(), path, recorded, built) for path in current)


def _recorded_notes(recorded: dict[str, str]) -> set[str]:
    return {path for path in recorded if path.startswith("knowledge/notes/")}


def _answer_paths(data) -> list[str]:
    """The vault-relative paths an answer's rows cite."""
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [path for path in map(_row_path, rows) if path]


def _row_path(row: object) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("path") or "").replace("\\", "/")


def _changed_since(root: Path, path: Path, recorded: dict[str, str], built: int) -> bool:
    if _mtime_ns(path) <= built:
        return False
    relative = path.relative_to(root).as_posix()
    if relative in recorded:
        return file_sha256(path) != recorded[relative]
    from corpus_snapshot import memory_source_would_be_collected

    return memory_source_would_be_collected(root, path)


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(read_stable_bytes(path, MAX_KNOWLEDGE_PAGE_BYTES, label="indexed source")).hexdigest()
    except (OSError, ValueError):
        return None


@functools.lru_cache(maxsize=4)
def recorded_memory_digests(generation: str) -> dict[str, str] | None:
    """The memory sources a generation recorded, by path; None when it cannot be read."""
    manifest = _generation_manifest(generation)
    try:
        from evidence_graph import MAX_SOURCE_MANIFEST_BYTES

        value = json.loads(read_stable_bytes(manifest.with_name("source-manifest.json"), MAX_SOURCE_MANIFEST_BYTES, label="source manifest"))
        return {str(item["relative_path"]): str(item["sha256"]) for item in value["sources"] if _is_memory_path(str(item["relative_path"]))}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _is_memory_path(relative: str) -> bool:
    return relative.startswith(("knowledge/notes/", "knowledge/projects/"))


def requested_signals(trace: dict) -> tuple[str, ...]:
    """The signals the requested mode declares, and any that ran besides.

    Graph is not 'missing' when never asked for; dense that a planned GRAPH run
    added is reported, not dropped.
    """
    from retrieval import PROFILE_SIGNALS

    mode = str(trace.get("requested_mode") or "").upper()
    declared = trace.get("signals_requested") or PROFILE_SIGNALS.get(mode, ("lexical", "dense", "graph"))
    return tuple(dict.fromkeys((*declared, *trace.get("signals_used", ()))))


def _recall_trace(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    trace = data.get("retrieval_trace")
    return trace if isinstance(trace, dict) else None


def recall_components(data) -> dict:
    trace = _recall_trace(data)
    if trace is None:
        return {}
    generation = trace.get("corpus_generation")
    signals = set(trace.get("signals_used", []))
    answer = answer_freshness(generation, _answer_paths(data))
    components = {
        signal: {
            "generation": generation,
            "freshness": _signal_freshness(signal, signals, answer),
        }
        for signal in requested_signals(trace)
    }
    components["reranker"] = {
        "generation": generation,
        "freshness": _reranker_freshness(trace),
    }
    return components


def answer_generation(name: str, data) -> object:
    if name in RETRIEVAL_TOOLS:
        return (_recall_trace(data) or {}).get("corpus_generation")
    return None


def index_timestamp(name: str, data) -> str | None:
    """When the corpus behind this answer was read.

    Recall reads a generation, so its answer is as old as that generation's build.
    Context collects the Markdown at request time, so its answer is as old as that
    collection; its `corpus_generation` is a content hash, not a generation id.
    """
    if name == "get_context" and isinstance(data, dict):
        return data.get("collected_at")
    built = generation_built_ns(answer_generation(name, data))
    if built is None:
        return None
    return dt.datetime.fromtimestamp(built / 1e9, tz=dt.UTC).isoformat(timespec="seconds")


def _context_components(data) -> dict:
    if not isinstance(data, dict):
        return {}
    if "items" not in data:
        return {}
    return {
        "context_compiler": {
            "generation": data.get("corpus_generation"),
            "freshness": "fresh",
        }
    }


# Every navigation status says how fresh its answer is; only an answer computed
# from the current revision is fresh. `unsupported` has no answer at all, and an
# unknown status is not assumed fresh (audit 2026-09-27 C-19,
# docs/research/2026-09-27-an-answer-without-a-provider-is-not-fresh.md).
NAVIGATION_FRESHNESS = {
    "ok": "fresh",
    "partial": "fresh",
    "stale": "stale",
    "unsupported": "missing",
    "not_ready": "unknown",
    "timeout": "unknown",
    "error": "unknown",
}


def navigation_freshness(status) -> str:
    return NAVIGATION_FRESHNESS.get(str(getattr(status, "value", status)), "unknown")


def _navigation_provider_component(data: dict, freshness: str) -> dict:
    provider = data.get("provider")
    if not isinstance(provider, dict):
        return {}
    if not isinstance(provider.get("name"), str):
        return {}
    return {
        "provider": {"generation": provider.get("version"), "freshness": freshness}
    }


def _first_graph_provenance(provenance: list):
    for item in provenance:
        if isinstance(item, dict) and item.get("source") == "graph":
            return item
    return None


def _navigation_graph_component(data: dict, freshness: str) -> dict:
    provenance = data.get("provenance")
    if not isinstance(provenance, list):
        return {}
    graph = _first_graph_provenance(provenance)
    if graph is None:
        return {}
    return {"graph": {"generation": graph.get("version"), "freshness": freshness}}


def _navigation_components(data: dict) -> dict:
    freshness = navigation_freshness(data.get("status"))
    return {
        **_navigation_provider_component(data, freshness),
        **_navigation_graph_component(data, freshness),
    }


_GRAPH_COMPONENT_KEYS = ("source_generation", "graph_complete", "fallback")


def _graph_component_freshness(data: dict) -> str:
    """What the answer's own freshness block says, not a constant.

    It said `fresh` beside `stale_by_commit: true` (audit B-34,
    docs/research/2026-09-25-a-structural-answer-says-its-own-freshness.md).
    """
    if "error" in data:
        return "unknown"
    return _block_freshness(data.get("freshness"))


def _block_freshness(block) -> str:
    if not isinstance(block, dict) or "unavailable" in block:
        return "unknown"
    return "stale" if block.get("stale_by_commit") else "fresh"


def graph_components(data) -> dict:
    if not isinstance(data, dict):
        return {}
    if not any(key in data for key in _GRAPH_COMPONENT_KEYS):
        return {}
    return {
        "graph": {
            "generation": data.get("source_generation"),
            "freshness": _graph_component_freshness(data),
        }
    }


_COMPONENT_BUILDERS = {
    "recall": recall_components,
    "get_decisions": recall_components,
    "get_context": _context_components,
    "get_architecture": graph_components,
    "find_dead_code": graph_components,
}


def components_for(name: str, data) -> dict[str, dict[str, object]]:
    if _is_rendered_navigation_data(name, data):
        return _navigation_components(data)
    builder = _COMPONENT_BUILDERS.get(name)
    if builder is None:
        return {}
    return builder(data)
