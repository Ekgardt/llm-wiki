"""Code graph — tree-sitter based code intelligence for the vault.

Parses source code files into a knowledge graph: functions, classes,
imports, call edges. Stores results for graph queries and impact analysis.

The graph enables:
- "Who calls this function?" — caller analysis
- "What will break if I change this?" — impact analysis (blast radius)
- "Find dead code" — functions with zero callers

Languages: Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, Ruby,
PHP, C#, and Bash. Each grammar is optional and loaded only when needed.

Install: uv sync --extra code-graph
"""
from __future__ import annotations

import ast
import importlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path, PurePath

try:
    from .code_languages import CODE_LANGUAGE_BY_SUFFIX, language_for_path
    from .import_resolver import (
        EMPTY_REGISTRY,
        SymbolRegistry,
        build_python_symbol_registry,
        resolve_python_imports_and_calls,
    )
except ImportError:
    from code_languages import CODE_LANGUAGE_BY_SUFFIX, language_for_path
    from import_resolver import (
        EMPTY_REGISTRY,
        SymbolRegistry,
        build_python_symbol_registry,
        resolve_python_imports_and_calls,
    )

# Lazy-loaded tree-sitter parsers.
_ts: dict = {}  # language_name -> Parser

GRAMMAR_LOADERS = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "php": ("tree_sitter_php", "language_php"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "bash": ("tree_sitter_bash", "language"),
}

LANGUAGE_MAP = CODE_LANGUAGE_BY_SUFFIX

MAX_DERIVED_COMMUNITY_CACHE = 4
# The hotspot list is a ranking, not a dump. This repository has 10,607 nodes
# with at least one incoming call; a caller reads the head of that ranking. The
# bound is stated in the answer as `hotspot_limit` / `hotspots_truncated`, so a
# reader is never left to mistake the top of the list for the whole of it.
HOTSPOT_LIMIT = 100
# Bound for the folded call-pair aggregate that feeds community detection.
# 200,000 against this repository's measured 29,868 pairs — 6.7x headroom.
MAX_CALL_PAIR_ROWS = 200_000
# Names `code_graph` treats as conventionally reached, pushed into the store's
# anti-join as caller data. Measured: it takes this repository's dead-code
# candidate set from 8,546 of 10,000 rows (17% headroom) to 3,621 (2.8x).
DEAD_CODE_NAME_PREFIXES = ("test_",)

CO_CHANGE_EDGE = "CO_CHANGED_WITH"
CODE_TOOLS_SCHEMA_VERSION = 1
_MANIFEST_WRITE_LOCK = threading.Lock()

QUERY_DIR = Path(__file__).with_name("queries")


def _have_tree_sitter() -> bool:
    """Check if tree-sitter is importable."""
    try:
        import tree_sitter  # noqa: F401
        return True
    except ImportError:
        return False


def _get_parser(lang: str):
    """Get or create a tree-sitter parser for a language. Returns None if unavailable."""
    if lang in _ts:
        return _ts[lang]

    try:
        import tree_sitter as ts

        loader = GRAMMAR_LOADERS.get(lang)
        if loader is None:
            return None
        module_name, factory_name = loader
        grammar = importlib.import_module(module_name)
        language = ts.Language(getattr(grammar, factory_name)())
        parser = ts.Parser(language)
        _ts[lang] = parser
        return parser
    except ImportError:
        return None
    except Exception:
        return None


def detect_language(file_path: Path) -> str | None:
    """Detect language from file extension."""
    return language_for_path(file_path)


def _probe_version(args: list[str], timeout: float = 2) -> tuple[str | None, str | None]:
    """Return a tool's first version line without invoking a shell."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    output = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not output:
        return None, f"version probe exited {result.returncode}"
    return output[0], None


def _command_tool(provider: str, path: str | None, args: list[str], semantic: bool) -> dict:
    if not path:
        return {
            "provider": provider, "available": False, "version": None, "path": None,
            "capabilities": {"semantic": semantic}, "failure": "executable not found",
        }
    version, failure = _probe_version([path, *args], timeout=2)
    return {
        "provider": provider,
        "available": failure is None,
        "version": version,
        "path": str(Path(path).resolve()),
        "capabilities": {"semantic": semantic},
        "failure": failure,
    }


def _jedi_tool() -> dict:
    """Jedi's availability, reported as one tool record either way."""
    try:
        version = metadata.version("jedi")
        importlib.import_module("jedi")
    except (ImportError, metadata.PackageNotFoundError) as exc:
        return {
            "provider": "jedi", "available": False, "version": None, "path": None,
            "capabilities": {"semantic": False},
            "failure": str(exc) or "package not found",
        }
    return {
        "provider": "jedi", "available": True, "version": version,
        "path": None, "capabilities": {"semantic": True}, "failure": None,
    }


def _tsc_names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("tsc.cmd", "tsc")
    return ("tsc", "tsc.cmd")


def _local_tsc(directory: Path) -> Path | None:
    binaries = directory / "node_modules" / ".bin"
    candidates = (binaries / name for name in _tsc_names())
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _tsc_command(directory: Path) -> str | None:
    """A workspace-local tsc outranks one on PATH."""
    local = _local_tsc(directory)
    if local is None:
        return shutil.which("tsc")
    return str(local)


def _command_tools(directory: Path) -> dict:
    """Probe the optional command-line servers in parallel."""
    specifications = {
        "typescript": ("typescript", _tsc_command(directory), ["--version"], False),
        "rust": ("rust-analyzer", shutil.which("rust-analyzer"), ["--version"], False),
        "go": ("gopls", shutil.which("gopls"), ["version"], False),
    }
    with ThreadPoolExecutor(max_workers=len(specifications)) as pool:
        futures = {
            name: pool.submit(_command_tool, *specification)
            for name, specification in specifications.items()
        }
        return {name: futures[name].result() for name in specifications}


def _manifest_destination(cache_path: Path | None) -> Path:
    if cache_path is not None:
        return cache_path
    try:
        from . import memory_state
    except ImportError:
        import memory_state
    return memory_state.STATE_ROOT / "cache" / "code_tools.json"


def detect_code_tools(directory: Path, cache_path: Path | None = None) -> dict:
    """Detect optional semantic tools and atomically refresh their manifest."""
    from datetime import datetime, timezone

    directory = directory.resolve()
    manifest = {
        "schema_version": CODE_TOOLS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tools": {"python": _jedi_tool(), **_command_tools(directory)},
    }
    _write_tool_manifest(_manifest_destination(cache_path), manifest)
    return manifest


_MANIFEST_REPLACE_ATTEMPTS = 20


def _staged_manifest(path: Path, manifest: dict) -> Path:
    """Write the manifest to a sibling temp file, removing it if the write fails.

    The caller owns the returned path, so a failed write must not leave behind a
    file whose name nobody holds: clean up here and re-raise.
    """
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f"{path.name}.", suffix=".tmp", delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _sleep_before_retry(attempt: int) -> None:
    if attempt < _MANIFEST_REPLACE_ATTEMPTS - 1:
        time.sleep(0.01)


def _replace_with_retry(temporary: Path, path: Path) -> None:
    """Windows refuses os.replace while another writer still holds the target."""
    for attempt in range(_MANIFEST_REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            _sleep_before_retry(attempt)


def _write_tool_manifest(path: Path, manifest: dict) -> None:
    """Atomically replace a manifest using a writer-unique sibling temp file."""
    with _MANIFEST_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _staged_manifest(path, manifest)
        try:
            _replace_with_retry(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _jedi_script(file_path: Path, workspace_root: Path):
    """The Jedi script for this file, or None when Jedi cannot be used at all."""
    try:
        jedi = importlib.import_module("jedi")
        project = jedi.Project(path=str(workspace_root.resolve()))
        return jedi.Script(path=str(file_path.resolve()), project=project)
    except Exception:
        return None


def _workspace_definition(definition, workspace: Path) -> tuple | None:
    """One inferred definition as a (full_name, path) pair inside the workspace."""
    module_path = getattr(definition, "module_path", None)
    full_name = getattr(definition, "full_name", None)
    if not module_path or not full_name:
        return None
    resolved_path = Path(module_path).resolve()
    resolved_path.relative_to(workspace)
    return (full_name, resolved_path)


def _inferred_targets(script, call: dict, workspace: Path) -> set:
    """The distinct workspace targets Jedi infers for one call site."""
    try:
        definitions = script.infer(line=call["line"], column=call.get("column", 0))
        pairs = (_workspace_definition(item, workspace) for item in definitions)
        return {pair for pair in pairs if pair is not None}
    except Exception:
        return set()


def _semantically_resolvable(call: dict) -> bool:
    return call.get("confidence") == "unknown" and call.get("semantic_eligible", False)


def _enriched_call(script, call: dict, workspace: Path) -> dict:
    """A call resolved to its one workspace target, or the call unchanged."""
    if not _semantically_resolvable(call):
        return call
    unique = _inferred_targets(script, call, workspace)
    if len(unique) != 1:
        return call
    full_name, _ = unique.pop()
    updated = dict(call)
    updated.update(qualified_name=full_name, confidence="confirmed", evidence="jedi")
    return updated


def enrich_python_semantics(file_path: Path, calls: list[dict], workspace_root: Path) -> list[dict]:
    """Resolve unknown Python calls with Jedi when it yields one workspace target."""
    script = _jedi_script(file_path, workspace_root)
    if script is None:
        return calls
    workspace = workspace_root.resolve()
    return [_enriched_call(script, call, workspace) for call in calls]


def _co_change_pathspec(directory: Path, repo_root: Path) -> str | None:
    try:
        pathspec = directory.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None
    return pathspec or "."


def _git_name_status_output(
    repo_root: Path, pathspec: str, timeout: float
) -> bytes | None:
    try:
        result = subprocess.run(  # noqa: S603, S607
            [
                "git", "log", "--reverse", "--max-count=2000", "--no-merges",
                "--name-status", "-z",
                "--find-renames=50%", "--find-copies=50%", "--format=COMMIT%x00%H",
                "--", pathspec,
            ],
            cwd=str(repo_root),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def _pairs_in_commit(files) -> list[tuple]:
    ordered = sorted(files)
    return [
        (source, target)
        for index, source in enumerate(ordered)
        for target in ordered[index + 1:]
    ]


def _shared_commit_counts(normalized: list) -> Counter:
    shared: Counter = Counter()
    for files in normalized:
        shared.update(_pairs_in_commit(files))
    return shared


def _co_change_measures(
    together: int, source_count: int, target_count: int, total: int
) -> tuple[float, float, float]:
    """Ochiai, lift, and NPMI for one pair of files changed together."""
    ochiai = together / math.sqrt(source_count * target_count)
    support = together / total
    lift = together * total / (source_count * target_count)
    denominator = -math.log(support)
    npmi = 0.0
    if denominator:
        npmi = math.log(lift) / denominator
    return ochiai, lift, npmi


def _co_change_edge(
    source: str,
    target: str,
    together: int,
    file_commits: Counter,
    total: int,
    preferred: dict,
    min_ochiai: float,
) -> dict | None:
    source_count = file_commits[source]
    target_count = file_commits[target]
    ochiai, lift, npmi = _co_change_measures(
        together, source_count, target_count, total
    )
    if ochiai < min_ochiai or lift <= 1 or npmi <= 0:
        return None
    return {
        "source": preferred.get(source, source),
        "target": preferred.get(target, target),
        "type": CO_CHANGE_EDGE,
        "weight": round(ochiai, 6),
        "ochiai": round(ochiai, 6),
        "npmi": round(npmi, 6),
        "lift": round(lift, 6),
        "support": round(together / total, 6),
        "shared_commits": together,
    }


def _bounded_commits(commits: list, max_commit_files: int) -> list:
    return [
        identities
        for identities in commits
        if 1 <= len(identities) <= max_commit_files
    ]


def _co_change_history(directory: Path, timeout: float):
    """Rename-aware git history for this directory, or None when unavailable."""
    repo_root = _git_repo_root(directory, timeout) or directory.resolve()
    pathspec = _co_change_pathspec(directory, repo_root)
    if pathspec is None:
        return None
    stdout = _git_name_status_output(repo_root, pathspec, timeout)
    if stdout is None:
        return None
    return _parse_git_name_status(stdout)


def _admitted_co_change_edge(
    pair, together, file_commits, total, preferred, min_shared_commits, min_ochiai
):
    if together < min_shared_commits:
        return None
    source, target = pair
    return _co_change_edge(
        source, target, together, file_commits, total, preferred, min_ochiai
    )


def _co_change_edges(
    normalized: list, preferred: dict, min_shared_commits: int, min_ochiai: float
) -> list[dict]:
    file_commits = Counter(identity for files in normalized for identity in files)
    total = len(normalized)
    edges = []
    for pair, together in _shared_commit_counts(normalized).items():
        edge = _admitted_co_change_edge(
            pair, together, file_commits, total, preferred,
            min_shared_commits, min_ochiai,
        )
        if edge is not None:
            edges.append(edge)
    return edges


def analyze_co_changes(
    directory: Path,
    *,
    min_shared_commits: int = 3,
    max_commit_files: int = 50,
    min_ochiai: float = 0.5,
    timeout: float = 10,
) -> list[dict]:
    """Find statistically meaningful file-level logical coupling in git history."""
    history = _co_change_history(directory, timeout)
    if history is None:
        return []
    commits, preferred = history
    normalized = _bounded_commits(commits, max_commit_files)
    edges = _co_change_edges(normalized, preferred, min_shared_commits, min_ochiai)
    return sorted(
        edges, key=lambda edge: (-edge["weight"], edge["source"], edge["target"])
    )


def _usable_git_output(result) -> bool:
    return result.returncode == 0 and bool(result.stdout) and b"\0" not in result.stdout


def _repo_root_from_output(result) -> Path | None:
    if not _usable_git_output(result):
        return None
    root = Path(result.stdout.decode("utf-8", errors="surrogateescape").strip())
    if not root.is_absolute():
        return None
    return root.resolve()


def _git_repo_root(directory: Path, timeout: float) -> Path | None:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(directory),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _repo_root_from_output(result)


class _GitIdentities:
    """Path-to-identity allocation that follows renames across commits."""

    def __init__(self) -> None:
        self.active: dict[str, int] = {}
        self.preferred: dict[int, str] = {}
        self._next = 0

    def new(self, path: str) -> int:
        identity = self._next
        self._next += 1
        self.active[path] = identity
        self.preferred[identity] = path
        return identity

    def rename(self, old_path: str, new_path: str) -> int:
        identity = self.active.pop(old_path, None)
        if identity is None:
            return self.new(new_path)
        self.active[new_path] = identity
        self.preferred[identity] = new_path
        return identity

    def existing(self, path: str) -> int:
        identity = self.active.get(path)
        if identity is None:
            return self.new(path)
        return identity


def _starts_commit(fields: list[str], index: int, record: str) -> bool:
    return record == "COMMIT" and index + 1 < len(fields)


def _outside_commit(fields: list[str], index: int, current) -> bool:
    return not fields[index] or current is None


def _append_git_change(fields: list[str], index: int, status: str, current) -> int:
    if status.startswith(("R", "C")) and index + 2 < len(fields):
        old_path, new_path = fields[index + 1:index + 3]
        current.append(
            (status, _normalize_git_path(old_path), _normalize_git_path(new_path))
        )
        return index + 3
    if index + 1 < len(fields):
        current.append((status, _normalize_git_path(fields[index + 1])))
        return index + 2
    return index + 1


def _scan_git_field(fields: list[str], index: int, records: list, current):
    record = fields[index].lstrip("\r\n")
    if _starts_commit(fields, index, record):
        started: list[tuple[str, ...]] = []
        records.append(started)
        return started, index + 2
    if _outside_commit(fields, index, current):
        return current, index + 1
    return current, _append_git_change(fields, index, record, current)


def _git_status_records(fields: list[str]) -> list:
    """Group the -z name-status stream into one change list per commit."""
    records: list[list[tuple[str, ...]]] = []
    current: list[tuple[str, ...]] | None = None
    index = 0
    while index < len(fields):
        current, index = _scan_git_field(fields, index, records, current)
    return records


def _folded_plain_change(change: tuple[str, ...], identities) -> int:
    path = change[1]
    identity = identities.existing(path)
    if change[0].startswith("D"):
        identities.active.pop(path, None)
    return identity


def _folded_change(change: tuple[str, ...], identities) -> int:
    status = change[0]
    if status.startswith("R"):
        return identities.rename(change[1], change[2])
    if status.startswith("C"):
        return identities.new(change[2])
    return _folded_plain_change(change, identities)


def _parse_git_name_status(data: bytes) -> tuple[list[set[int]], dict[int, str]]:
    fields = [
        field.decode("utf-8", errors="surrogateescape") for field in data.split(b"\0")
    ]
    records = _git_status_records(fields)
    identities = _GitIdentities()
    commits = [
        {_folded_change(change, identities) for change in changes}
        for changes in records
    ]
    return commits, identities.preferred


def _normalize_git_path(path: str) -> str:
    return path.replace("\\", "/")


def _co_change_root(directory: Path | None) -> Path | None:
    if directory is None:
        return None
    return _git_repo_root(directory, 10) or directory.resolve()


def _coupling_key(source: object, target: object, root: Path | None) -> frozenset:
    return frozenset((
        _normalize_edge_path(source, root), _normalize_edge_path(target, root)
    ))


def _coupling_index(co_change_edges: list[dict], root: Path | None) -> dict:
    return {
        _coupling_key(edge["source"], edge["target"], root): edge
        for edge in co_change_edges
        if edge.get("type") == CO_CHANGE_EDGE
    }


def _carries_co_change(edge: dict, match: dict | None) -> bool:
    return (
        edge.get("type") == "CALLS"
        and edge.get("confidence") == "confirmed"
        and bool(match)
    )


def _refined_call_edge(edge: dict, coupling: dict, root: Path | None) -> dict:
    updated = dict(edge)
    match = coupling.get(
        _coupling_key(edge.get("source", ""), edge.get("target", ""), root)
    )
    if not _carries_co_change(edge, match):
        return updated
    evidence = dict(edge.get("evidence", {}))
    evidence["co_change_weight"] = match["weight"]
    updated["evidence"] = evidence
    return updated


def refine_call_edges_with_co_changes(
    call_edges: list[dict], co_change_edges: list[dict], directory: Path | None = None
) -> list[dict]:
    """Add co-change evidence to confirmed calls without changing edge semantics."""
    root = _co_change_root(directory)
    coupling = _coupling_index(co_change_edges, root)
    return [_refined_call_edge(edge, coupling, root) for edge in call_edges]


def _normalize_edge_path(path: str, root: Path | None) -> str:
    normalized = _normalize_git_path(path)
    candidate = Path(normalized)
    if root and candidate.is_absolute():
        try:
            normalized = candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            pass
    return _normalize_git_path(normalized)


def _git_log_line(file_path: Path) -> str:
    """The one-line git record for this file, or empty when git cannot answer."""
    parent = file_path.parent
    cwd = str(parent) if parent.exists() else None
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["git", "log", "-1", "--format=%H|%cI|%an", "--", str(file_path)],
            capture_output=True, text=True, timeout=5, cwd=cwd,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _get_git_info(file_path: Path) -> dict:
    """Get git commit info for a file (bi-temporal tracking).

    Returns dict with commit_hash, commit_date, author.
    Falls back to empty strings if not in a git repo.
    """
    line = _git_log_line(file_path)
    if not line:
        return {"commit_hash": "", "commit_date": "", "author": ""}
    padded = (*line.split("|"), "", "")
    return {
        "commit_hash": padded[0],
        "commit_date": padded[1],
        "author": padded[2],
    }


def parse_file(file_path: Path) -> dict:
    """Parse a single source file and extract symbols.

    Returns dict with:
        file: str (relative path)
        language: str
        functions: list[{name, line, end_line}]
        classes: list[{name, line, end_line}]
        calls: list[{name, line}]
        imports: list[{name, line}]
    """
    registry = (
        build_python_symbol_registry(file_path.parent)
        if detect_language(file_path) == "python"
        else EMPTY_REGISTRY
    )
    return _parse_file(file_path, registry, file_path.parent)


def _empty_parse_result(file_path: Path) -> dict:
    return {
        "file": str(file_path), "language": None, "functions": [],
        "classes": [], "calls": [], "imports": [],
    }


def _language_symbols(file_path, lang, registry, workspace_root, extracted):
    """Python calls/imports come from the resolver, not the tree-sitter query."""
    functions, classes, calls, imports = extracted
    if lang != "python":
        return functions, classes, calls, imports
    imports, calls = resolve_python_imports_and_calls(
        file_path, registry, workspace_root
    )
    calls = enrich_python_semantics(file_path, calls, workspace_root)
    return functions, classes, calls, imports


def _tree_sitter_symbols(file_path: Path, lang: str, registry, workspace_root):
    """Extracted symbols, or None when the caller must fall back to regex."""
    parser = _get_parser(lang)
    if parser is None:
        return None
    source = file_path.read_bytes()
    extracted = _extract_symbols(parser.parse(source), parser.language, lang, source)
    if extracted is None:
        return None
    return _language_symbols(file_path, lang, registry, workspace_root, extracted)


def _parse_file(file_path: Path, registry: SymbolRegistry, workspace_root: Path) -> dict:
    lang = detect_language(file_path)
    if not lang:
        return _empty_parse_result(file_path)
    symbols = _tree_sitter_symbols(file_path, lang, registry, workspace_root)
    if symbols is None:
        # Fallback: regex-based extraction (less accurate but no deps).
        return _regex_parse(file_path, lang, registry, workspace_root)
    functions, classes, calls, imports = symbols
    # Bi-temporal: attach git commit info (valid_from = commit date).
    git_info = _get_git_info(file_path)
    return {
        "file": str(file_path),
        "language": lang,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "imports": imports,
        "git_commit": git_info["commit_hash"],
        "valid_from": git_info["commit_date"],
        "author": git_info["author"],
    }


def _query_matches(tree, language, lang: str):
    """The query matches for this language, or None when it cannot run."""
    try:
        import tree_sitter as ts

        query_source = (QUERY_DIR / f"{lang}.scm").read_text(encoding="utf-8")
        return ts.QueryCursor(ts.Query(language, query_source)).matches(tree.root_node)
    except (OSError, UnicodeError, Exception):
        return None


def _as_node_list(value) -> list:
    if isinstance(value, list):
        return value
    return [value]


_IMPORT_WRAPPERS = (("'", "'"), ('"', '"'), ("<", ">"))


def _unquoted_import(text: str) -> str:
    """An import path may arrive quoted or angle-bracketed; the name is inside."""
    if len(text) < 2:
        return text
    for opener, closer in _IMPORT_WRAPPERS:
        if text[0] == opener and text[-1] == closer:
            return text[1:-1]
    return text


def _symbol_signature(kind: str, owner, source: bytes, text: str) -> dict:
    if kind != "function":
        return {}
    declaration = source[owner.start_byte:owner.end_byte].decode(
        "utf-8", errors="ignore"
    )
    signature = _declaration_signature(declaration, text)
    if not signature:
        return {}
    return {"signature": signature}


def _symbol_record(kind: str, node, owner, source: bytes) -> dict:
    text = source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
    if kind == "import":
        text = _unquoted_import(text)
    return {
        "name": text,
        "line": owner.start_point[0] + 1,
        "end_line": owner.end_point[0] + 1,
        "column": owner.start_point[1],
        "end_column": owner.end_point[1],
        **_symbol_signature(kind, owner, source, text),
    }


def _collect_symbol_kind(
    kind: str, captures: dict, source: bytes, group: list, seen: set
) -> None:
    nodes = _as_node_list(captures.get(f"{kind}.name", []))
    owners = _as_node_list(captures.get(f"{kind}.node", nodes))
    for index, node in enumerate(nodes):
        owner = node
        if owners:
            owner = owners[min(index, len(owners) - 1)]
        key = (node.start_byte, node.end_byte, kind)
        if key in seen:
            continue
        seen.add(key)
        group.append(_symbol_record(kind, node, owner, source))


_SYMBOL_KINDS = ("function", "class", "call", "import")


def _collect_all_symbol_kinds(matches, source: bytes) -> dict:
    groups = {name: [] for name in _SYMBOL_KINDS}
    seen = {name: set() for name in _SYMBOL_KINDS}
    for _, captures in matches:
        for kind in _SYMBOL_KINDS:
            _collect_symbol_kind(kind, captures, source, groups[kind], seen[kind])
    return groups


def _extract_symbols(tree, language, lang: str, source: bytes) -> tuple | None:
    """Execute the language query and return functions, classes, calls, imports."""
    matches = _query_matches(tree, language, lang)
    if matches is None:
        return None
    groups = _collect_all_symbol_kinds(matches, source)
    return tuple(groups[name] for name in _SYMBOL_KINDS)


_PYTHON_CALL_KEYWORDS = {"if", "for", "while", "def", "class", "print"}
_SCRIPT_CALL_KEYWORDS = {"if", "for", "while", "switch", "catch"}


def _regex_python_definitions(
    line: str, number: int, functions: list, classes: list
) -> None:
    match = re.match(r"\s*def\s+(\w+)", line)
    if match:
        functions.append(_regex_function(match.group(1), line, number))
    match = re.match(r"\s*class\s+(\w+)", line)
    if match:
        classes.append({"name": match.group(1), "line": number, "end_line": number})


def _regex_python_call(line: str, number: int, calls: list) -> None:
    match = re.match(r"\s*(\w+)\s*\(", line)
    if match and match.group(1) not in _PYTHON_CALL_KEYWORDS:
        calls.append({"name": match.group(1), "line": number})


def _regex_python_import(line: str, number: int, imports: list) -> None:
    match = re.match(r"\s*(?:from\s+\S+\s+)?import\s+(\w+)", line)
    if match:
        imports.append({"name": match.group(1), "line": number})


def _regex_parse_python_line(
    line: str, number: int, functions: list, classes: list, calls: list, imports: list
) -> None:
    _regex_python_definitions(line, number, functions, classes)
    _regex_python_call(line, number, calls)
    _regex_python_import(line, number, imports)


def _regex_parse_python(
    content: str,
    file_path: Path,
    registry: SymbolRegistry,
    workspace_root: Path,
    functions: list,
    classes: list,
) -> tuple[list, list]:
    """Python calls and imports come from the resolver, not from the regex pass."""
    calls: list = []
    imports: list = []
    for number, line in enumerate(content.splitlines(), 1):
        _regex_parse_python_line(
            line, number, functions, classes, calls, imports
        )
    imports, calls = resolve_python_imports_and_calls(
        file_path, registry, workspace_root
    )
    return imports, enrich_python_semantics(file_path, calls, workspace_root)


def _script_declared_names(line: str) -> set[str]:
    return {
        match.group(1)
        for match in [
            re.match(r"\s*(?:export\s+)?function\s+(\w+)", line),
            re.match(r"\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::[^{}]+)?\{", line),
        ]
        if match
    }


def _regex_script_definitions(
    line: str, number: int, functions: list, classes: list
) -> None:
    match = re.match(r"\s*(?:export\s+)?function\s+(\w+)", line)
    if match:
        functions.append(_regex_function(match.group(1), line, number))
    match = re.match(r"\s*(?:export\s+)?class\s+(\w+)", line)
    if match:
        classes.append({"name": match.group(1), "line": number, "end_line": number})


def _regex_script_arrow(line: str, number: int, functions: list) -> None:
    match = re.match(r"\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", line)
    if match:
        functions.append(_regex_function(match.group(1), line, number))


def _regex_script_calls(line: str, number: int, calls: list) -> None:
    declared = _script_declared_names(line)
    for call in re.finditer(r"\b(\w+)\s*\(", line):
        name = call.group(1)
        if name not in declared and name not in _SCRIPT_CALL_KEYWORDS:
            calls.append({"name": name, "line": number})


def _regex_parse_script_line(
    line: str, number: int, functions: list, classes: list, calls: list
) -> None:
    _regex_script_definitions(line, number, functions, classes)
    _regex_script_arrow(line, number, functions)
    _regex_script_calls(line, number, calls)


def _regex_parse_script(
    content: str, functions: list, classes: list, calls: list
) -> None:
    for number, line in enumerate(content.splitlines(), 1):
        _regex_parse_script_line(line, number, functions, classes, calls)


def _regex_parse_by_language(
    content: str,
    file_path: Path,
    lang: str,
    registry: SymbolRegistry,
    workspace_root: Path,
    functions: list,
    classes: list,
    calls: list,
    imports: list,
) -> tuple[list, list]:
    if lang == "python":
        return _regex_parse_python(
            content, file_path, registry, workspace_root, functions, classes
        )
    if lang in ("javascript", "typescript"):
        _regex_parse_script(content, functions, classes, calls)
        return imports, calls
    _regex_parse_additional_languages(
        content, lang, functions, classes, calls, imports
    )
    return imports, calls


def _regex_parse(
    file_path: Path, lang: str, registry: SymbolRegistry, workspace_root: Path
) -> dict:
    """Fallback: regex-based symbol extraction (no tree-sitter required)."""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    functions: list = []
    classes: list = []
    calls: list = []
    imports: list = []
    imports, calls = _regex_parse_by_language(
        content,
        file_path,
        lang,
        registry,
        workspace_root,
        functions,
        classes,
        calls,
        imports,
    )
    git_info = _get_git_info(file_path)
    return {
        "file": str(file_path),
        "language": lang,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "imports": imports,
        "git_commit": git_info["commit_hash"],
        "valid_from": git_info["commit_date"],
        "author": git_info["author"],
    }


_ADDITIONAL_LANGUAGE_PATTERNS = {
    "go": (r"\bfunc\s+(?:\([^)]*\)\s*)?(\w+)\s*\(", r"\btype\s+(\w+)\s+(?:struct|interface)\b"),
    "rust": (r"\bfn\s+(\w+)\s*\(", r"\b(?:struct|enum|union|trait)\s+(\w+)\b"),
    "java": (r"\b(?:void|[A-Z]\w*|\w+(?:<[^>]+>)?)\s+(\w+)\s*\([^;]*\)\s*\{", r"\b(?:class|interface|enum)\s+(\w+)\b"),
    "c": (r"\b(?:void|int|char|float|double|\w+\s*\*)\s+(\w+)\s*\([^;]*\)\s*\{", r"\b(?:struct|union)\s+(\w+)\b"),
    "cpp": (r"\b(?:void|int|char|float|double|auto|\w+(?:::\w+)*\s*\*?)\s+(\w+)\s*\([^;]*\)\s*\{", r"\b(?:class|struct|union)\s+(\w+)\b"),
    "ruby": (r"^\s*def\s+(?:self\.)?(\w+[!?=]?)", r"^\s*(?:class|module)\s+([A-Z]\w*)"),
    "php": (r"\bfunction\s+(\w+)\s*\(", r"\b(?:class|interface|trait)\s+(\w+)\b"),
    "c_sharp": (r"\b(?:void|[A-Z]\w*|\w+(?:<[^>]+>)?)\s+(\w+)\s*\([^;]*\)\s*\{", r"\b(?:class|interface|struct|enum)\s+(\w+)\b"),
    "bash": (r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{", None),
}
_ADDITIONAL_CALL_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "class", "struct", "interface",
})
_ADDITIONAL_COMMAND_KEYWORDS = frozenset({
    "class", "def", "do", "else", "elsif", "end", "fi", "function",
    "if", "load", "module", "require", "source", "then",
})


def _regex_additional_function(
    line: str, number: int, pattern: str, functions: list[dict], declared: set
) -> None:
    match = re.search(pattern, line)
    if not match:
        return
    name = match.group(1)
    declared.add(name)
    functions.append(_regex_function(name, line, number))


def _regex_additional_class(
    line: str, number: int, pattern: str | None, classes: list[dict]
) -> None:
    if pattern is None:
        return
    match = re.search(pattern, line)
    if match is None:
        return
    classes.append({"name": match.group(1), "line": number, "end_line": number})


def _append_additional_call(
    name: str, number: int, declared: set, keywords: frozenset, calls: list[dict]
) -> None:
    if name in declared or name in keywords:
        return
    calls.append({"name": name, "line": number})


def _additional_call_name(name: str, lang: str) -> str:
    if lang == "rust":
        return name.removesuffix("!")
    return name


def _regex_additional_calls(
    line: str, number: int, lang: str, declared: set, calls: list[dict]
) -> None:
    for call in re.finditer(r"\b(?:\w+(?:::|\.|->))?(\w+[!?]?)\s*[!(]", line):
        name = _additional_call_name(call.group(1), lang)
        _append_additional_call(
            name, number, declared, _ADDITIONAL_CALL_KEYWORDS, calls
        )


def _regex_additional_commands(
    line: str, number: int, lang: str, declared: set, calls: list[dict]
) -> None:
    if lang not in {"ruby", "bash"}:
        return
    for command in re.finditer(r"(?:^|[;{])\s*([A-Za-z_]\w*[!?]?)\s+", line):
        _append_additional_call(
            command.group(1), number, declared, _ADDITIONAL_COMMAND_KEYWORDS, calls
        )


def _regex_additional_line(
    line: str, number: int, lang: str, patterns: tuple, declared: set, sinks: dict
) -> None:
    _regex_additional_function(line, number, patterns[0], sinks["functions"], declared)
    _regex_additional_class(line, number, patterns[1], sinks["classes"])
    _regex_add_import(line, number, lang, sinks["imports"])
    _regex_additional_calls(line, number, lang, declared, sinks["calls"])
    _regex_additional_commands(line, number, lang, declared, sinks["calls"])


def _regex_parse_additional_languages(
    content: str,
    lang: str,
    functions: list[dict],
    classes: list[dict],
    calls: list[dict],
    imports: list[dict],
) -> None:
    """Extract basic symbols for optional grammars when tree-sitter is absent."""
    patterns = _ADDITIONAL_LANGUAGE_PATTERNS[lang]
    declared: set = set()
    sinks = {
        "functions": functions, "classes": classes,
        "calls": calls, "imports": imports,
    }
    for number, line in enumerate(content.splitlines(), 1):
        _regex_additional_line(line, number, lang, patterns, declared, sinks)


def _regex_function(name: str, declaration: str, line: int) -> dict:
    function = {"name": name, "line": line, "end_line": line}
    signature = _declaration_signature(declaration, name)
    if signature:
        function["signature"] = signature
    return function


def _paren_delta(char: str) -> int:
    if char == "(":
        return 1
    if char == ")":
        return -1
    return 0


def _closing_paren_index(declaration: str, start: int) -> int | None:
    depth = 0
    for index in range(start, len(declaration)):
        depth += _paren_delta(declaration[index])
        if depth == 0:
            return index
    return None


def _declaration_signature(declaration: str, name: str) -> str | None:
    """Return a compact name-and-parameters signature when it is explicit."""
    match = re.search(rf"\b{re.escape(name)}\s*\(", declaration)
    if not match:
        return None
    start = declaration.find("(", match.start())
    end = _closing_paren_index(declaration, start)
    if end is None:
        return None
    parameters = re.sub(r"\s+", " ", declaration[start:end + 1]).strip()
    parameters = re.sub(r"\s*,\s*", ", ", parameters)
    return f"{name}{parameters}"


def _regex_add_import(line: str, line_number: int, lang: str, imports: list[dict]) -> None:
    patterns = {
        "go": r'^\s*import\s+(?:\w+\s+)?["`]([^"`]+)["`]',
        "rust": r"^\s*use\s+([^;]+)",
        "java": r"^\s*import\s+(?:static\s+)?([^;]+)",
        "c": r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]",
        "cpp": r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]",
        "ruby": r"^\s*(?:require|load)\s*[('\"]+([^)'\"]+)",
        "php": r"^\s*(?:use\s+([^;]+)|(?:include|include_once|require|require_once)\s*[('\"]+([^)'\"]+))",
        "c_sharp": r"^\s*using\s+([^;=]+)",
        "bash": r"^\s*(?:source|\.)\s+([^\s;]+)",
    }
    if match := re.search(patterns[lang], line):
        name = next(group for group in match.groups() if group is not None)
        imports.append({"name": name.strip(), "line": line_number})


_INDEX_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})


def _indexable_source(path: Path, extensions: set) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in extensions:
        return False
    return not any(skip in path.parts for skip in _INDEX_SKIP_DIRS)


def _accumulate_index_stats(stats: dict, result: dict) -> None:
    if not result["language"]:
        return
    stats["files"] += 1
    for key in ("functions", "classes", "calls", "imports"):
        stats[key] += len(result[key])


def _print_index_stats(stats: dict) -> None:
    ts_status = "tree-sitter" if _have_tree_sitter() else "regex fallback"
    print(f"Indexed {stats['files']} files ({ts_status}):")
    print(f"  Functions: {stats['functions']}")
    print(f"  Classes:   {stats['classes']}")
    print(f"  Calls:     {stats['calls']}")
    print(f"  Imports:   {stats['imports']}")


def index_directory(directory: Path, verbose: bool = True) -> dict:
    """Index all source files in a directory.

    Returns stats: {files, functions, classes, calls, imports}
    """
    detect_code_tools(directory)
    stats = {"files": 0, "functions": 0, "classes": 0, "calls": 0, "imports": 0}
    if not directory.exists():
        return stats
    extensions = set(LANGUAGE_MAP.keys())
    registry = build_python_symbol_registry(directory)
    for path in sorted(directory.rglob("*")):
        if _indexable_source(path, extensions):
            _accumulate_index_stats(stats, _parse_file(path, registry, directory))
    if verbose:
        _print_index_stats(stats)
    return stats


def _valid_monotonic_deadline(deadline: object) -> bool:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    return math.isfinite(deadline)


def _require_valid_deadline(deadline: object) -> None:
    if not _valid_monotonic_deadline(deadline):
        raise ValueError("deadline must be an absolute monotonic timestamp or None")


def _require_generation_deadline(deadline: float | None) -> None:
    if deadline is None:
        return
    _require_valid_deadline(deadline)
    if time.monotonic() >= deadline:
        raise TimeoutError("generation catalog deadline reached")


def _check_generation_stop(deadline: float | None, cancelled) -> None:
    _require_generation_deadline(deadline)
    if cancelled is not None and cancelled():
        raise TimeoutError("generation catalog operation cancelled")


def _generation_state_root(memory_state) -> Path:
    configured = os.environ.get("LLM_WIKI_STATE_ROOT")
    if configured:
        return Path(configured).resolve()
    return memory_state.STATE_ROOT


def _bounded_call(factory, *args, deadline, cancelled):
    """Pass deadline/cancelled through only when the caller supplied either."""
    if deadline is None and cancelled is None:
        return factory(*args)
    return factory(*args, deadline=deadline, cancelled=cancelled)


def _new_generation_catalog(
    catalog_class, read_only, state_root, catalog_path, deadline, cancelled
):
    if not read_only:
        return catalog_class(state_root, catalog_path=catalog_path)
    if deadline is None and cancelled is None:
        return catalog_class.open_existing_read_only(
            state_root, catalog_path=catalog_path
        )
    return catalog_class.open_existing_read_only(
        state_root, catalog_path=catalog_path, deadline=deadline, cancelled=cancelled
    )


def _generation_catalog(
    directory: Path,
    *,
    read_only: bool = False,
    deadline: float | None = None,
    cancelled=None,
):
    """Open the shared generation catalog only when it already exists."""
    del directory
    if not isinstance(read_only, bool):
        raise TypeError("read_only must be a boolean")
    _check_generation_stop(deadline, cancelled)
    try:
        from . import memory_state
        from .generation_catalog import GenerationCatalog
    except ImportError:
        import memory_state
        from generation_catalog import GenerationCatalog

    state_root = _generation_state_root(memory_state)
    catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
    _check_generation_stop(deadline, cancelled)
    if not catalog_path.is_file():
        return None
    _check_generation_stop(deadline, cancelled)
    catalog = _new_generation_catalog(
        GenerationCatalog, read_only, state_root, catalog_path, deadline, cancelled
    )
    _check_generation_stop(deadline, cancelled)
    return catalog


def _generation_catalog_for(directory, read_only, deadline, cancelled):
    """Keep the plain one-argument call, which callers and tests substitute for."""
    if read_only or deadline is not None or cancelled is not None:
        return _generation_catalog(
            directory, read_only=read_only, deadline=deadline, cancelled=cancelled
        )
    return _generation_catalog(directory)


def _active_graph_or_none(
    directory, read_only, deadline, cancelled, graph_class, resolve_scope
):
    _check_generation_stop(deadline, cancelled)
    catalog = _generation_catalog_for(directory, read_only, deadline, cancelled)
    if catalog is None:
        return None
    scope = _bounded_call(
        resolve_scope, directory, deadline=deadline, cancelled=cancelled
    )
    _check_generation_stop(deadline, cancelled)
    return _bounded_call(
        graph_class.open_active_for_repository, catalog, scope,
        deadline=deadline, cancelled=cancelled,
    )


def _active_evidence_graph(
    directory: Path,
    *,
    read_only: bool = False,
    deadline: float | None = None,
    cancelled=None,
):
    try:
        from .evidence_graph import EvidenceGraph
        from .repository_scope import resolve_repository_scope
    except ImportError:
        from evidence_graph import EvidenceGraph
        from repository_scope import resolve_repository_scope

    try:
        return _active_graph_or_none(
            directory, read_only, deadline, cancelled,
            EvidenceGraph, resolve_repository_scope,
        )
    except TimeoutError:
        raise
    except (OSError, TypeError, ValueError, PermissionError, sqlite3.Error):
        return None


def _stored_location(graph, node_id: str, _directory: Path) -> tuple[str, int]:
    source_root = Path(graph.repository_scope.checkout_root)
    occurrences = graph.occurrences(node_id, max_rows=1)
    if not occurrences:
        node = graph.node(node_id)
        path = "" if node is None else str(node["metadata"].get("path", ""))
        return (str(source_root / path) if path else "", 0)
    occurrence = occurrences[0]
    return str(source_root / occurrence["relative_path"]), occurrence["line_start"]


def _stored_edge_location(graph, assertion_id: str, _directory: Path) -> tuple[str, int]:
    evidence = graph.evidence(assertion_id=assertion_id, max_rows=1)
    if not evidence:
        return "", 0
    span = evidence[0]
    source_root = Path(graph.repository_scope.checkout_root)
    return str(source_root / span["relative_path"]), span["line_start"]


def _stored_qualified_name(node: dict[str, object]) -> str:
    owner = str(node["metadata"].get("owner", ""))
    name = str(node["metadata"].get("name", node["identity_key"]))
    return f"{owner}.{name}" if owner else name


def _stored_architecture_node(graph, node: dict[str, object], directory: Path) -> dict:
    location = _stored_location(graph, node["node_id"], directory)
    return {
        **node["metadata"],
        "node_id": node["node_id"],
        "file": location[0],
        "line": location[1],
    }


def _add_undirected_edge(graph: dict, source: str, target: str, weight: float) -> None:
    if source == target:
        return
    graph.setdefault(source, {})[target] = graph.setdefault(source, {}).get(target, 0) + weight
    graph.setdefault(target, {})[source] = graph.setdefault(target, {}).get(source, 0) + weight


def _undirected_call_graph(edges: list[dict[str, object]]) -> dict:
    """Fold resolved call edges into one weighted undirected graph.

    Accepts either a row of record from `EvidenceGraph.edges()`, which carries
    no weight and counts as one, or a pair already folded in SQL by
    `EvidenceGraph.edge_weights()`, which carries how many assertions joined it.
    """
    graph: dict[str, dict[str, float]] = {}
    for edge in edges:
        _add_undirected_edge(
            graph,
            str(edge["source_node_id"]),
            str(edge["target_node_id"]),
            float(edge.get("weight", 1)),
        )
    return graph


def _stored_call_pairs(graph_reader) -> list[dict[str, object]]:
    """The whole call graph as distinct weighted undirected pairs, folded in SQL.

    Measured on this repository: 35,313 resolved CALLS assertions fold into
    29,868 pairs, 0.30 s, 4.07 MB as a Python adjacency, 1.93 s of Louvain.
    The bound is `MAX_CALL_PAIR_ROWS`; above it the reader refuses by name.
    """
    return graph_reader.edge_weights(
        edge_types=("CALLS",), max_rows=MAX_CALL_PAIR_ROWS
    )


def _derived_cache(graph_reader) -> dict:
    cache = getattr(graph_reader, "_derived_code_graph_cache", None)
    if cache is None:
        cache = {}
        graph_reader._derived_code_graph_cache = cache
    return cache


def _stored_communities(graph_reader, edges: list[dict[str, object]]) -> list[list[str]]:
    cache = _derived_cache(graph_reader)
    cache_key = "communities/calls/v1"
    if cache_key in cache:
        return cache[cache_key]
    communities = _louvain_communities(_undirected_call_graph(edges))
    if len(cache) >= MAX_DERIVED_COMMUNITY_CACHE:
        cache.pop(next(iter(cache)))
    cache[cache_key] = communities
    return communities


def _store_report(graph) -> dict[str, object]:
    unresolved_count = graph._database.execute(
        "SELECT count(*) FROM observation"
    ).fetchone()[0]
    return {
        "source_generation": graph.generation_id,
        "source_scope": "checkout",
        "source_root": graph.repository_scope.checkout_root,
        "graph_complete": unresolved_count == 0,
        "unresolved_count": unresolved_count,
        "fallback": False,
    }


def _live_unresolved_count(
    directory: Path, parsed: list[tuple[Path, dict]] | None = None
) -> int:
    if parsed is None:
        parsed, _definitions, _edges = _workspace_call_graph(directory)
    return sum(
        1
        for _path, result in parsed
        for call in result["calls"]
        if call.get("confidence") not in {"confirmed", "heuristic"}
    )


def _live_report(
    directory: Path, parsed: list[tuple[Path, dict]] | None = None
) -> dict[str, object]:
    return {
        "source_generation": None,
        "graph_complete": False,
        "unresolved_count": _live_unresolved_count(directory, parsed),
        "fallback": True,
    }


def _with_report(key: str, value, report: dict[str, object], enabled: bool):
    return {key: value, **report} if enabled else value


def _stored_callers(
    function_name: str, directory: Path, with_report: bool
) -> list[dict] | dict | None:
    if with_report:
        return _store_find_callers(function_name, directory, with_report=True)
    return _store_find_callers(function_name, directory)


_SEARCH_SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv"}


def _searchable_source(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in set(LANGUAGE_MAP.keys()):
        return False
    return not any(skip in path.parts for skip in _SEARCH_SKIP_PARTS)


def _named_function(result: dict, function_name: str) -> dict | None:
    return next(
        (item for item in result["functions"] if item["name"] == function_name), None
    )


def _callees_in_function(path: Path, result: dict, func_def: dict) -> list[dict]:
    callees = []
    for call in result["calls"]:
        end_line = func_def.get("end_line", call["line"])
        if func_def["line"] <= call["line"] <= end_line:
            callees.append(
                {"file": str(path), "line": call["line"], "callee": call["name"]}
            )
    return callees


def _caller_edge(path: Path, call: dict, function_name: str) -> dict:
    return {
        "file": str(path),
        "line": call["line"],
        "function": function_name,
        "qualified_name": call.get("qualified_name"),
        "confidence": call.get("confidence", "heuristic"),
    }


def _call_names_target(call: dict, function_name: str, language: str) -> bool:
    """Unknown Python calls are excluded; other languages fall back to the name."""
    resolved = (call.get("qualified_name") or call["name"]).rsplit(".", 1)[-1]
    if resolved != function_name:
        return False
    return call.get("confidence") == "confirmed" or language != "python"


def _live_callers_in_file(path: Path, result: dict, function_name: str) -> list[dict]:
    return [
        _caller_edge(path, call, function_name)
        for call in result["calls"]
        if _call_names_target(call, function_name, result["language"])
    ]


def find_callers(
    function_name: str,
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
) -> list[dict] | dict:
    """Find callers using strict Python evidence or non-Python name heuristics.

    Unknown Python calls are excluded. Returns caller edge dictionaries.
    """
    if not live:
        stored = _stored_callers(function_name, directory, with_report)
        if stored is not None:
            return stored
    callers: list[dict] = []
    registry = build_python_symbol_registry(directory)
    for path in sorted(directory.rglob("*")):
        if not _searchable_source(path):
            continue
        result = _parse_file(path, registry, directory)
        callers.extend(_live_callers_in_file(path, result, function_name))
    return _with_report("callers", callers, _live_report(directory), with_report)


def _stored_caller_row(graph, edge, function_name: str, directory: Path) -> dict | None:
    caller = graph.node(edge["source_node_id"])
    if caller is None:
        return None
    location = _stored_edge_location(graph, edge["assertion_id"], directory)
    return {
        "file": location[0],
        "line": location[1],
        "function": function_name,
        "qualified_name": _stored_qualified_name(caller),
        "confidence": edge["confidence"],
        "symbol_id": caller["node_id"],
    }


def _sorted_stored_rows(rows: list, key) -> list[dict]:
    return sorted([row for row in rows if row is not None], key=key)


def _caller_sort_key(item: dict):
    return (item["file"], item["line"], item["symbol_id"])


def _store_find_callers(
    function_name: str, directory: Path, *, with_report: bool = False
) -> list[dict] | dict | None:
    graph = _active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        targets = graph.find_nodes(
            kinds=("function", "method"), name=function_name, max_rows=10_000
        )
        target_ids = sorted({item["node_id"] for item in targets})
        edges = graph.edges(
            edge_types=("CALLS",), target_node_ids=target_ids, max_rows=10_000
        )
        rows = [
            _stored_caller_row(graph, edge, function_name, directory) for edge in edges
        ]
        results = _sorted_stored_rows(rows, _caller_sort_key)
        return _with_report("callers", results, _store_report(graph), with_report)
    finally:
        graph.close()


def _stored_callees(
    function_name: str, directory: Path, with_report: bool
) -> list[dict] | dict | None:
    if with_report:
        return _store_find_callees(function_name, directory, with_report=True)
    return _store_find_callees(function_name, directory)


def _live_callees_in_file(
    path: Path, registry, directory: Path, function_name: str
) -> list[dict]:
    if not _searchable_source(path):
        return []
    result = _parse_file(path, registry, directory)
    func_def = _named_function(result, function_name)
    if func_def is None:
        return []
    return _callees_in_function(path, result, func_def)


def find_callees(
    function_name: str,
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
) -> list[dict] | dict:
    """Find all functions called BY a function (CALLS edge, forward direction).

    Returns list of {file, line, callee}.
    """
    if not live:
        stored = _stored_callees(function_name, directory, with_report)
        if stored is not None:
            return stored
    callees: list[dict] = []
    registry = build_python_symbol_registry(directory)
    for path in sorted(directory.rglob("*")):
        callees.extend(_live_callees_in_file(path, registry, directory, function_name))
    return _with_report("callees", callees, _live_report(directory), with_report)


def _stored_callee_row(graph, edge, directory: Path) -> dict | None:
    callee = graph.node(edge["target_node_id"])
    if callee is None:
        return None
    location = _stored_edge_location(graph, edge["assertion_id"], directory)
    return {
        "file": location[0],
        "line": location[1],
        "callee": callee["metadata"].get("name", callee["identity_key"]),
        "symbol_id": callee["node_id"],
        "confidence": edge["confidence"],
    }


def _callee_sort_key(item: dict):
    return (str(item["callee"]), item["symbol_id"])


def _store_find_callees(
    function_name: str, directory: Path, *, with_report: bool = False
) -> list[dict] | dict | None:
    graph = _active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        sources = graph.find_nodes(
            kinds=("function", "method"), name=function_name, max_rows=10_000
        )
        source_ids = sorted({item["node_id"] for item in sources})
        edges = graph.edges(
            edge_types=("CALLS",), source_node_ids=source_ids, max_rows=10_000
        )
        rows = [_stored_callee_row(graph, edge, directory) for edge in edges]
        results = _sorted_stored_rows(rows, _callee_sort_key)
        return _with_report("callees", results, _store_report(graph), with_report)
    finally:
        graph.close()


def _reachable_or_conventional(function, name, path, incoming, exports) -> bool:
    return (
        function["symbol_id"] in incoming
        or name in {"main", "__init__"}
        or name.startswith("test_")
        or path.name.startswith("test_")
        or name in exports
    )


def _live_dead_candidate(
    function: dict, path: Path, incoming, exports, lines
) -> dict | None:
    name = function["name"]
    if _reachable_or_conventional(function, name, path, incoming, exports):
        return None
    if _is_framework_route(lines, function["line"]):
        return None
    return {
        "name": name,
        "symbol_id": function["symbol_id"],
        "owner": function["owner"],
        "file": str(path),
        "line": function["line"],
        "status": "candidate",
        "reason": "zero_confirmed_incoming_calls",
        "graph_complete": False,
    }


def _live_dead_candidates_in_file(path: Path, result: dict, incoming) -> list[dict]:
    source = path.read_text(encoding="utf-8", errors="ignore")
    exports = _declared_exports(path, source, result["language"])
    lines = source.splitlines()
    found = [
        _live_dead_candidate(function, path, incoming, exports, lines)
        for function in result["functions"]
    ]
    return [item for item in found if item is not None]


def _stored_dead_code_result(directory: Path, with_report: bool):
    if with_report:
        return _store_find_dead_code(directory, with_report=True)
    return _store_find_dead_code(directory)


def find_dead_code(
    directory: Path, *, live: bool = False, with_report: bool = False
) -> list[dict] | dict:
    """Return conservative dead-code candidates from the incomplete static graph."""
    if not live:
        stored = _stored_dead_code_result(directory, with_report)
        if stored is not None:
            return stored
    parsed, definitions, edges = _workspace_call_graph(directory)
    incoming = {edge["target"] for edge in edges}
    candidates: list[dict] = []
    for path, result in parsed:
        candidates.extend(_live_dead_candidates_in_file(path, result, incoming))
    candidates = sorted(
        candidates, key=lambda item: (item["name"], item["file"], item["line"])
    )
    return _with_report(
        "candidates", candidates, _live_report(directory, parsed), with_report
    )


def _conventionally_reachable(name: str, path: str) -> bool:
    """Names this project treats as reached even with no confirmed caller.

    The `test_` name prefix is also pushed into the store's anti-join; the
    basename rule stays here, where `PurePath(path).name` means exactly what it
    says and the nearest SQL spelling would mean something wider.
    """
    return (
        name in {"main", "__init__"}
        or name.startswith("test_")
        or PurePath(path).name.startswith("test_")
    )


def _stored_dead_candidate(graph, node: dict, directory: Path) -> dict | None:
    name = str(node["metadata"].get("name", ""))
    path = str(node["metadata"].get("path", ""))
    if _conventionally_reachable(name, path):
        return None
    location = _stored_location(graph, node["node_id"], directory)
    return {
        "name": name,
        "symbol_id": node["node_id"],
        "owner": node["metadata"].get("owner", ""),
        "file": location[0],
        "line": location[1],
        "status": "candidate",
        "reason": "zero_confirmed_incoming_calls",
        "graph_complete": False,
    }


def _stored_dead_candidates(graph, nodes, directory) -> list[dict]:
    found = [_stored_dead_candidate(graph, node, directory) for node in nodes]
    return [item for item in found if item is not None]


def _stored_dead_nodes(graph) -> list[dict]:
    """Function and method nodes no resolved CALLS reaches and no EXPOSES names.

    The anti-join runs in SQL, so this reads its own answer — measured 3,621
    rows in 0.17 s — instead of the 19,153 nodes and 35,313 edges the old shape
    materialised before refusing at the row ceiling.
    """
    return graph.nodes_without_edges(
        kinds=("function", "method"),
        incoming_edge_types=("CALLS",),
        outgoing_edge_types=("EXPOSES",),
        exclude_name_prefixes=DEAD_CODE_NAME_PREFIXES,
        max_rows=10_000,
    )


def _marked_complete(candidates: list[dict], report: dict) -> list[dict]:
    for candidate in candidates:
        candidate["graph_complete"] = report["graph_complete"]
    return sorted(
        candidates, key=lambda item: (item["name"], item["file"], item["line"])
    )


def _store_find_dead_code(
    directory: Path, *, with_report: bool = False
) -> list[dict] | dict | None:
    graph = _active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        candidates = _stored_dead_candidates(graph, _stored_dead_nodes(graph), directory)
        report = _store_report(graph)
        return _with_report(
            "candidates", _marked_complete(candidates, report), report, with_report
        )
    finally:
        graph.close()


def _all_assignment(node) -> bool:
    return isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__all__"
        for target in node.targets
    )


def _all_names(node) -> set[str]:
    try:
        value = ast.literal_eval(node.value)
    except (ValueError, TypeError):
        return set()
    return {item for item in value if isinstance(item, str)}


def _python_exports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    for node in tree.body:
        if _all_assignment(node):
            return _all_names(node)
    return set()


def _script_exports(source: str) -> set[str]:
    return set(
        re.findall(
            r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function|class)\s+(\w+)",
            source,
        )
    )


def _declared_exports(path: Path, source: str, language: str) -> set[str]:
    if language == "python":
        return _python_exports(source)
    if language in {"javascript", "typescript"}:
        return _script_exports(source)
    return set()


def _is_framework_route(lines: list[str], definition_line: int) -> bool:
    prefix = "\n".join(lines[max(0, definition_line - 2):definition_line])
    return bool(re.search(r"(?:@\w*\.route\s*\(|@(?:GetMapping|RequestMapping)\b)", prefix))


def _main_entry_points(path: Path, result: dict) -> list[dict]:
    return [
        {"kind": "main", "name": "main", "file": str(path), "line": function["line"]}
        for function in result["functions"]
        if function["name"] == "main"
    ]


def _live_architecture_points(parsed) -> tuple[list, list]:
    entry_points: list[dict] = []
    routes: list[dict] = []
    for path, result in parsed:
        source = path.read_text(encoding="utf-8", errors="ignore")
        entry_points.extend(_main_entry_points(path, result))
        entry_points.extend(_listen_entry_points(path, source))
        routes.extend(_framework_routes(path, source))
    return entry_points, routes


def _live_hotspots(incoming: dict, definitions: dict) -> list[dict]:
    hotspots = [
        {
            "name": definitions[symbol_id]["name"],
            "symbol_id": symbol_id,
            "owner": definitions[symbol_id]["owner"],
            "file": definitions[symbol_id]["file"],
            "line": definitions[symbol_id]["line"],
            "incoming_callers": len(callers),
        }
        for symbol_id, callers in incoming.items()
        if symbol_id in definitions
    ]
    hotspots.sort(
        key=lambda item: (-item["incoming_callers"], item["name"], item["file"])
    )
    return hotspots


def _live_incoming(edges: list[dict]) -> dict:
    incoming: dict[str, set[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["target"], set()).add(edge["source"])
    return incoming


def get_architecture(
    directory: Path, *, live: bool = False, with_report: bool = False
) -> dict:
    """Summarize statically visible entry points, routes, hotspots, and modules."""
    if not live:
        stored = _store_get_architecture(directory)
        if stored is not None:
            return stored
    parsed, definitions, edges = _workspace_call_graph(directory)
    entry_points, routes = _live_architecture_points(parsed)
    architecture = {
        "entry_points": entry_points,
        "routes": routes,
        **_hotspot_fields(*_bounded_hotspots(
            _live_hotspots(_live_incoming(edges), definitions)
        )),
        "communities": _communities_from_edges(edges),
        "graph_complete": False,
    }
    return {**architecture, **_live_report(directory, parsed)}


def _bounded_hotspots(hotspots: list[dict]) -> tuple[list[dict], bool]:
    return hotspots[:HOTSPOT_LIMIT], len(hotspots) > HOTSPOT_LIMIT


def _hotspot_fields(hotspots: list[dict], truncated: bool) -> dict:
    """The hotspot ranking with its bound stated in the answer, never implied."""
    return {
        "hotspots": hotspots,
        "hotspot_limit": HOTSPOT_LIMIT,
        "hotspots_truncated": truncated,
    }


def _stored_hotspot(graph, node_id: str, node, incoming: int, directory: Path) -> dict:
    location = _stored_location(graph, node_id, directory)
    return {
        "name": node["metadata"].get("name", node["identity_key"]),
        "symbol_id": node_id,
        "owner": node["metadata"].get("owner", ""),
        "file": location[0],
        "line": location[1],
        "incoming_callers": incoming,
    }


def _stored_hotspot_row(graph, row: dict, directory: Path) -> dict | None:
    node_id = str(row["node_id"])
    node = graph.node(node_id)
    if node is None:
        return None
    return _stored_hotspot(graph, node_id, node, int(row["incoming"]), directory)


def _stored_hotspots(graph, directory: Path) -> tuple[list[dict], bool]:
    """The top of the caller ranking, counted by a GROUP BY rather than in Python.

    Measured on this repository: the ranking has 10,607 members and the top 100
    of it costs 0.10 s, against a refusal for the old shape, which pulled every
    function node and every call edge to count in Python.
    """
    counts, truncated = graph.top_incoming_edge_counts(
        edge_types=("CALLS",), kinds=("function", "method"), max_rows=HOTSPOT_LIMIT
    )
    found = [_stored_hotspot_row(graph, row, directory) for row in counts]
    hotspots = [item for item in found if item is not None]
    hotspots.sort(
        key=lambda item: (-item["incoming_callers"], str(item["name"]), item["file"])
    )
    return hotspots, truncated


def _stored_architecture_nodes(graph, nodes, directory: Path) -> list[dict]:
    return [_stored_architecture_node(graph, node, directory) for node in nodes]


def _store_get_architecture(directory: Path) -> dict | None:
    graph = _active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        entries = graph.find_nodes(kinds=("entry-point",), max_rows=10_000)
        routes = graph.find_nodes(kinds=("route",), max_rows=10_000)
        report = _store_report(graph)
        return {
            "entry_points": _stored_architecture_nodes(graph, entries, directory),
            "routes": _stored_architecture_nodes(graph, routes, directory),
            **_hotspot_fields(*_stored_hotspots(graph, directory)),
            "communities": _stored_communities(graph, _stored_call_pairs(graph)),
            **report,
        }
    finally:
        graph.close()


def _listen_entry_points(path: Path, source: str) -> list[dict]:
    return [
        {
            "kind": "listen", "name": match.group(1), "file": str(path),
            "line": source.count("\n", 0, match.start()) + 1,
        }
        for match in re.finditer(r"\b((?:app|server)\.listen)\s*\(", source)
    ]


def _framework_routes(path: Path, source: str) -> list[dict]:
    patterns = [
        (r"@\w+\.route\s*\(\s*['\"]([^'\"]+)", "ROUTE"),
        (r"\b(?:app|router)\.(get|post|put|patch|delete|all)\s*\(\s*['\"]([^'\"]+)", None),
        (r"@(Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*['\"]([^'\"]+)", None),
    ]
    routes = []
    for pattern, fixed_method in patterns:
        for match in re.finditer(pattern, source, re.IGNORECASE):
            method = fixed_method or match.group(1).upper().removesuffix("MAPPING")
            route_path = match.group(1 if fixed_method else 2)
            routes.append({
                "method": method, "path": route_path, "file": str(path),
                "line": source.count("\n", 0, match.start()) + 1,
            })
    return routes


def detect_communities(
    directory: Path, *, live: bool = False, with_report: bool = False
) -> list[list[str]] | dict:
    """Detect functional modules with deterministic weighted Louvain."""
    if not live:
        stored = (
            _store_detect_communities(directory, with_report=True)
            if with_report
            else _store_detect_communities(directory)
        )
        if stored is not None:
            return stored
    communities = _detect_live_communities(directory)
    return _with_report("communities", communities, _live_report(directory), with_report)


def _detect_live_communities(directory: Path) -> list[list[str]]:
    _, _, edges = _workspace_call_graph(directory)
    return _communities_from_edges(edges)


def _communities_from_edges(edges: list[dict]) -> list[list[str]]:
    graph: dict[str, dict[str, float]] = {}
    for edge in edges:
        caller, callee = edge["source"], edge["target"]
        if caller == callee:
            continue
        graph.setdefault(caller, {})[callee] = graph.setdefault(caller, {}).get(callee, 0) + 1
        graph.setdefault(callee, {})[caller] = graph.setdefault(callee, {}).get(caller, 0) + 1
    return _louvain_communities(graph)


def _store_detect_communities(
    directory: Path, *, with_report: bool = False
) -> list[list[str]] | dict | None:
    graph = _active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        communities = _stored_communities(graph, _stored_call_pairs(graph))
        return _with_report("communities", communities, _store_report(graph), with_report)
    finally:
        graph.close()


def find_dependencies(
    node_id: str,
    directory: Path,
    *,
    reverse: bool = False,
    live: bool = False,
    with_report: bool = False,
) -> list[dict] | dict:
    """Find bounded canonical dependencies, preferring the active generation."""
    if not live:
        graph = _active_evidence_graph(directory)
        if graph is not None:
            try:
                dependencies = graph.dependencies(
                    node_id, reverse=reverse, max_depth=8, max_rows=10_000
                )
                return _with_report(
                    "dependencies", dependencies, _store_report(graph), with_report
                )
            finally:
                graph.close()
    parsed, definitions, edges = _workspace_call_graph(directory)
    dependencies = _find_live_dependencies(
        node_id, definitions, edges, reverse=reverse
    )
    return _with_report(
        "dependencies", dependencies, _live_report(directory, parsed), with_report
    )


def find_paths(
    source_node_id: str,
    target_node_id: str,
    directory: Path,
    *,
    live: bool = False,
    with_report: bool = False,
) -> list[dict] | dict:
    """Find bounded canonical graph paths, preferring the active generation."""
    if not live:
        graph = _active_evidence_graph(directory)
        if graph is not None:
            try:
                paths = graph.path(
                    source_node_id,
                    target_node_id,
                    max_depth=8,
                    max_rows=10,
                    max_work=10_000,
                )
                return _with_report("paths", paths, _store_report(graph), with_report)
            finally:
                graph.close()
    parsed, definitions, edges = _workspace_call_graph(directory)
    paths = _find_live_paths(source_node_id, target_node_id, definitions, edges)
    return _with_report(
        "paths", paths, _live_report(directory, parsed), with_report
    )


def _live_node_ids(value: str, definitions: dict[str, dict]) -> list[str]:
    if value in definitions:
        return [value]
    by_name: dict[str, list[str]] = {}
    for identifier, definition in definitions.items():
        by_name.setdefault(str(definition["name"]), []).append(identifier)
    return sorted(by_name.get(value, []))


def _dependency_direction(reverse: bool) -> tuple[str, str]:
    if reverse:
        return ("target", "source")
    return ("source", "target")


def _dependency_adjacency(edges, start_key: str, target_key: str) -> dict:
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge[start_key], []).append(edge[target_key])
    return adjacency


def _dependency_record(target: str, definition: dict, depth: int) -> dict:
    return {
        "node_id": target,
        "kind": "function",
        "identity_scheme": "live-code/v1",
        "identity_key": target,
        "metadata": {
            "name": definition["name"],
            "owner": definition.get("owner", ""),
            "path": definition.get("file", ""),
        },
        "depth": depth,
    }


def _visit_dependency(target, depth, definitions, seen, results, pending) -> None:
    if target in seen:
        return
    seen.add(target)
    definition = definitions.get(target)
    if definition is not None:
        results.append(_dependency_record(target, definition, depth))
    pending.append((target, depth))


def _expand_dependency(
    current, depth, adjacency, definitions, seen, results, pending
) -> None:
    if depth >= 8:
        return
    for target in sorted(adjacency.get(current, [])):
        _visit_dependency(target, depth + 1, definitions, seen, results, pending)


def _dependency_budget_left(results: list, work: int) -> bool:
    return len(results) < 10_000 and work < 10_000


def _find_live_dependencies(
    node_id: str,
    definitions: dict[str, dict],
    edges: list[dict],
    *,
    reverse: bool,
) -> list[dict]:
    start_key, target_key = _dependency_direction(reverse)
    adjacency = _dependency_adjacency(edges, start_key, target_key)
    pending = [(identifier, 0) for identifier in _live_node_ids(node_id, definitions)]
    seen = {identifier for identifier, _depth in pending}
    results: list[dict] = []
    work = 0
    while pending and _dependency_budget_left(results, work):
        current, depth = pending.pop(0)
        work += 1
        _expand_dependency(
            current, depth, adjacency, definitions, seen, results, pending
        )
    return results


def _path_outgoing(edges) -> dict:
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge["target"])
    return outgoing


def _reached_target(node, path, targets) -> bool:
    return node in targets and len(path) > 1


def _queue_path(target, path, pending) -> None:
    if target in path:
        return
    pending.append((target, [*path, target]))


def _extend_paths(node, path, outgoing, pending) -> None:
    if len(path) > 8:
        return
    for target in sorted(outgoing.get(node, [])):
        _queue_path(target, path, pending)


def _path_budget_left(paths: list, work: int) -> bool:
    return len(paths) < 10 and work < 10_000


def _find_live_paths(
    source_node_id: str,
    target_node_id: str,
    definitions: dict[str, dict],
    edges: list[dict],
) -> list[dict]:
    sources = _live_node_ids(source_node_id, definitions)
    targets = set(_live_node_ids(target_node_id, definitions))
    outgoing = _path_outgoing(edges)
    pending = [(source, [source]) for source in sorted(sources)]
    paths: list[dict] = []
    work = 0
    while pending and _path_budget_left(paths, work):
        node, path = pending.pop(0)
        work += 1
        if _reached_target(node, path, targets):
            paths.append(
                {"node_ids": path, "assertion_ids": [], "depth": len(path) - 1}
            )
            continue
        _extend_paths(node, path, outgoing, pending)
    return paths


_WORKSPACE_SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def _parsable_workspace_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in LANGUAGE_MAP:
        return False
    return not any(skip in path.parts for skip in _WORKSPACE_SKIP_PARTS)


def _index_workspace_definitions(
    path: Path,
    result: dict,
    directory: Path,
    definitions: dict[str, dict],
    by_name: dict[str, list[dict]],
    by_qualified: dict[str, dict],
) -> None:
    for function in result["functions"]:
        definition = {**function, "file": str(path), "language": result["language"]}
        definitions[function["symbol_id"]] = definition
        by_name.setdefault(function["name"], []).append(definition)
        if result["language"] == "python":
            qualified = _python_qualified_name(path, function, directory)
            by_qualified[qualified] = definition


def _unambiguous_candidate(candidates: list[dict], path: Path) -> dict | None:
    same_file = [item for item in candidates if item["file"] == str(path)]
    if len(same_file) == 1:
        return same_file[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolved_python_target(call, by_qualified, by_name, path):
    """An unconfirmed Python call stays unresolved; it never falls back to a name."""
    if call.get("confidence") != "confirmed":
        return None
    confirmed = by_qualified.get(call.get("qualified_name"))
    if confirmed is not None:
        return confirmed
    return _unambiguous_candidate(by_name.get(call["name"], []), path)


def _resolved_call_target(
    call: dict,
    path: Path,
    language: str,
    by_name: dict[str, list[dict]],
    by_qualified: dict[str, dict],
) -> dict | None:
    """The one definition this call can name, or None when it stays ambiguous."""
    if language == "python":
        return _resolved_python_target(call, by_qualified, by_name, path)
    return _unambiguous_candidate(by_name.get(call["name"], []), path)


def _call_edge(path: Path, result: dict, call: dict, by_name, by_qualified) -> dict | None:
    caller = _containing_function(result["functions"], call)
    if caller is None:
        return None
    target = _resolved_call_target(
        call, path, result["language"], by_name, by_qualified
    )
    if target is None:
        return None
    return {"source": caller["symbol_id"], "target": target["symbol_id"]}


def _workspace_call_edges(
    parsed: list[tuple[Path, dict]],
    by_name: dict[str, list[dict]],
    by_qualified: dict[str, dict],
) -> list[dict]:
    edges = []
    for path, result in parsed:
        found = [
            _call_edge(path, result, call, by_name, by_qualified)
            for call in result["calls"]
        ]
        edges.extend(item for item in found if item is not None)
    return edges


def _workspace_call_graph(
    directory: Path,
) -> tuple[list[tuple[Path, dict]], dict[str, dict], list[dict]]:
    """Parse a workspace and resolve calls to canonical path/owner/name IDs."""
    directory = directory.resolve()
    registry = build_python_symbol_registry(directory)
    parsed: list[tuple[Path, dict]] = []
    definitions: dict[str, dict] = {}
    by_name: dict[str, list[dict]] = {}
    by_qualified: dict[str, dict] = {}
    for path in sorted(directory.rglob("*")):
        if not _parsable_workspace_file(path):
            continue
        result = _parse_file(path, registry, directory)
        _annotate_function_ids(path, result, directory)
        parsed.append((path, result))
        _index_workspace_definitions(
            path, result, directory, definitions, by_name, by_qualified
        )
    return parsed, definitions, _workspace_call_edges(parsed, by_name, by_qualified)


def _function_owner(function: dict, containers: list[dict]) -> str:
    enclosing = [
        item for item in containers
        if item is not function and _symbol_contains(item, function)
    ]
    if not enclosing:
        return "<module>"
    return min(enclosing, key=_symbol_span)["name"]


def _annotated_identity(function: dict, containers: list[dict], relative: str) -> str:
    owner = _function_owner(function, containers)
    function["owner"] = owner
    identity = function.get("signature") or f"{function['name']}@L{function['line']}"
    return f"{relative}::{owner}::{identity}"


def _assign_symbol_id(function: dict, identity: str, duplicated: bool) -> None:
    if duplicated:
        function["symbol_id"] = f"{identity}@L{function['line']}"
        return
    function["symbol_id"] = identity


def _annotate_function_ids(path: Path, result: dict, root: Path) -> None:
    containers = [*result["classes"], *result["functions"]]
    relative = path.resolve().relative_to(root).as_posix()
    annotated = [
        (function, _annotated_identity(function, containers, relative))
        for function in result["functions"]
    ]
    counts = Counter(identity for _function, identity in annotated)
    for function, identity in annotated:
        _assign_symbol_id(function, identity, counts[identity] > 1)


def _python_qualified_name(path: Path, function: dict, root: Path) -> str:
    relative = path.resolve().relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    module = ".".join(parts)
    owner = "" if function["owner"] == "<module>" else f".{function['owner']}"
    return f"{module}{owner}.{function['name']}"


def _symbol_span(symbol: dict) -> tuple[int, int]:
    return (
        symbol.get("end_line", symbol["line"]) - symbol["line"],
        symbol.get("end_column", 0) - symbol.get("column", 0),
    )


def _symbol_contains(container: dict, child: dict) -> bool:
    start = (container["line"], container.get("column", 0))
    end = (container.get("end_line", container["line"]), container.get("end_column", 10**9))
    child_start = (child["line"], child.get("column", 0))
    child_end = (child.get("end_line", child["line"]), child.get("end_column", 10**9))
    return start <= child_start and child_end <= end and (start, end) != (child_start, child_end)


def _containing_function(functions: list[dict], call: dict) -> dict | None:
    matches = [function for function in functions if _symbol_contains(function, call)]
    return min(matches, key=_symbol_span) if matches else None


def _best_louvain_target(
    node: object,
    weights: dict[object, float],
    totals: dict[object, float],
    degree: dict[object, float],
    old: object,
    m2: float,
) -> object:
    best = old
    best_gain = 0.0
    for target in sorted(weights, key=str):
        gain = weights[target] - totals[target] * degree[node] / m2
        if gain > best_gain + 1e-12:
            best, best_gain = target, gain
    return best


def _neighbor_community_weights(
    neighbors: dict[object, float], community: dict[object, object]
) -> dict[object, float]:
    weights: dict[object, float] = {}
    for neighbor, weight in neighbors.items():
        target = community[neighbor]
        weights[target] = weights.get(target, 0.0) + weight
    return weights


def _move_one_node(
    node: object,
    current: dict,
    community: dict[object, object],
    totals: dict[object, float],
    degree: dict[object, float],
    m2: float,
) -> bool:
    """Move one node to its best community; True when it actually moved."""
    old = community[node]
    totals[old] -= degree[node]
    weights = _neighbor_community_weights(current[node], community)
    best = _best_louvain_target(node, weights, totals, degree, old, m2)
    community[node] = best
    totals[best] += degree[node]
    return best != old


def _local_louvain_pass(
    nodes: list,
    current: dict,
    community: dict[object, object],
    totals: dict[object, float],
    degree: dict[object, float],
    m2: float,
) -> None:
    moved = True
    while moved:
        moved = False
        for node in nodes:
            moved |= _move_one_node(node, current, community, totals, degree, m2)


def _grouped_communities(nodes: list, community: dict[object, object]) -> list[list]:
    groups: dict[object, list[object]] = {}
    for node in nodes:
        groups.setdefault(community[node], []).append(node)
    return sorted(
        (sorted(group, key=str) for group in groups.values()),
        key=lambda group: str(group[0]),
    )


def _aggregated_round(current: dict, members: dict, ordered_groups: list):
    group_of = {
        node: index for index, group in enumerate(ordered_groups) for node in group
    }
    grouped_members = {
        index: set().union(*(members[node] for node in group))
        for index, group in enumerate(ordered_groups)
    }
    return _aggregate_louvain_graph(current, group_of), grouped_members


def _louvain_round(current: dict, members: dict):
    """One aggregation round, or None when the partition has settled."""
    nodes = sorted(current, key=str)
    degree = {node: sum(current[node].values()) for node in nodes}
    m2 = sum(degree.values())
    if not m2:
        return None
    community = {node: node for node in nodes}
    _local_louvain_pass(nodes, current, community, dict(degree), degree, m2)
    ordered_groups = _grouped_communities(nodes, community)
    if len(ordered_groups) == len(nodes):
        return None
    return _aggregated_round(current, members, ordered_groups)


def _louvain_seed(graph: dict) -> tuple[dict, dict]:
    current = {node: dict(neighbors) for node, neighbors in graph.items()}
    return current, {node: {node} for node in current}


def _louvain_result(members: dict) -> list[list[str]]:
    communities = [sorted(group) for group in members.values() if len(group) >= 2]
    return sorted(communities, key=lambda group: group[0])


def _louvain_communities(graph: dict[str, dict[str, float]]) -> list[list[str]]:
    """Optimize modularity on a weighted undirected graph without dependencies."""
    current, members = _louvain_seed(graph)
    while current:
        advanced = _louvain_round(current, members)
        if advanced is None:
            break
        current, members = advanced
    return _louvain_result(members)


def _add_self_weight(reduced: dict, group: int, weight: float) -> None:
    reduced[group][group] = reduced[group].get(group, 0.0) + weight


def _add_group_weight(reduced: dict, left: int, right: int, weight: float) -> None:
    if left == right:
        _add_self_weight(reduced, left, 2 * weight)
        return
    reduced[left][right] = reduced[left].get(right, 0.0) + weight
    reduced[right][left] = reduced[right].get(left, 0.0) + weight


def _fold_louvain_pair(
    reduced: dict, group_of: dict, source, target, weight: float
) -> None:
    if source == target:
        _add_self_weight(reduced, group_of[source], weight)
        return
    if str(source) > str(target):
        return
    _add_group_weight(reduced, group_of[source], group_of[target], weight)


def _aggregate_louvain_graph(
    graph: dict[object, dict[object, float]], group_of: dict[object, int]
) -> dict[int, dict[int, float]]:
    """Aggregate an undirected weighted graph using adjacency degree weights."""
    reduced: dict[int, dict[int, float]] = {
        group: {} for group in sorted(set(group_of.values()))
    }
    for source in sorted(graph, key=str):
        for target, weight in graph[source].items():
            _fold_louvain_pair(reduced, group_of, source, target, weight)
    return reduced


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Code graph — tree-sitter code intelligence.")
    p.add_argument("directory", nargs="?", default=".", help="Directory to index.")
    p.add_argument("--callers", type=str, default=None, help="Find callers of a function.")
    args = p.parse_args()

    directory = Path(args.directory)

    if args.callers:
        callers = find_callers(args.callers, directory)
        print(f"Callers of '{args.callers}': {len(callers)} found.")
        for c in callers[:20]:
            print(f"  {c['file']}:{c['line']}")
        return 0

    index_directory(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
