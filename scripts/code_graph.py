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
    from .import_resolver import (
        EMPTY_REGISTRY,
        SymbolRegistry,
        build_python_symbol_registry,
        resolve_python_imports_and_calls,
    )
except ImportError:
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

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "c_sharp",
    ".sh": "bash",
    ".bash": "bash",
}

MAX_DERIVED_COMMUNITY_CACHE = 4

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
    return LANGUAGE_MAP.get(file_path.suffix.lower())


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


def detect_code_tools(directory: Path, cache_path: Path | None = None) -> dict:
    """Detect optional semantic tools and atomically refresh their manifest."""
    from datetime import datetime, timezone

    try:
        from . import memory_state
    except ImportError:
        import memory_state

    directory = directory.resolve()
    try:
        jedi_version = metadata.version("jedi")
        importlib.import_module("jedi")
        python = {
            "provider": "jedi", "available": True, "version": jedi_version,
            "path": None, "capabilities": {"semantic": True}, "failure": None,
        }
    except (ImportError, metadata.PackageNotFoundError) as exc:
        python = {
            "provider": "jedi", "available": False, "version": None, "path": None,
            "capabilities": {"semantic": False}, "failure": str(exc) or "package not found",
        }

    tsc_names = ("tsc.cmd", "tsc") if sys.platform == "win32" else ("tsc", "tsc.cmd")
    local_tsc = next(
        (
            candidate
            for name in tsc_names
            for candidate in (directory / "node_modules" / ".bin" / name,)
            if candidate.is_file()
        ),
        None,
    )
    tsc = str(local_tsc) if local_tsc else shutil.which("tsc")
    specifications = {
        "typescript": ("typescript", tsc, ["--version"], False),
        "rust": ("rust-analyzer", shutil.which("rust-analyzer"), ["--version"], False),
        "go": ("gopls", shutil.which("gopls"), ["version"], False),
    }
    with ThreadPoolExecutor(max_workers=len(specifications)) as pool:
        futures = {
            name: pool.submit(_command_tool, *specification)
            for name, specification in specifications.items()
        }
        tools = {"python": python}
        tools.update({name: futures[name].result() for name in specifications})
    manifest = {
        "schema_version": CODE_TOOLS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tools": tools,
    }
    destination = cache_path or memory_state.STATE_ROOT / "cache" / "code_tools.json"
    _write_tool_manifest(destination, manifest)
    return manifest


def _write_tool_manifest(path: Path, manifest: dict) -> None:
    """Atomically replace a manifest using a writer-unique sibling temp file."""
    with _MANIFEST_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f"{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(manifest, handle, indent=2, sort_keys=True)
                handle.write("\n")
            for attempt in range(20):
                try:
                    os.replace(temporary, path)
                    return
                except PermissionError:
                    if attempt < 19:
                        time.sleep(0.01)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def enrich_python_semantics(file_path: Path, calls: list[dict], workspace_root: Path) -> list[dict]:
    """Resolve unknown Python calls with Jedi when it yields one workspace target."""
    try:
        jedi = importlib.import_module("jedi")
        project = jedi.Project(path=str(workspace_root.resolve()))
        script = jedi.Script(path=str(file_path.resolve()), project=project)
    except Exception:
        return calls

    enriched = []
    workspace = workspace_root.resolve()
    for call in calls:
        if call.get("confidence") != "unknown" or not call.get("semantic_eligible", False):
            enriched.append(call)
            continue
        try:
            definitions = script.infer(line=call["line"], column=call.get("column", 0))
            candidates = []
            for definition in definitions:
                module_path = getattr(definition, "module_path", None)
                full_name = getattr(definition, "full_name", None)
                if not module_path or not full_name:
                    continue
                resolved_path = Path(module_path).resolve()
                resolved_path.relative_to(workspace)
                candidates.append((full_name, resolved_path))
            unique = set(candidates)
        except Exception:
            unique = set()
        if len(unique) == 1:
            updated = dict(call)
            full_name, _ = unique.pop()
            updated.update(qualified_name=full_name, confidence="confirmed", evidence="jedi")
            enriched.append(updated)
        else:
            enriched.append(call)
    return enriched


def analyze_co_changes(
    directory: Path,
    *,
    min_shared_commits: int = 3,
    max_commit_files: int = 50,
    min_ochiai: float = 0.5,
    timeout: float = 10,
) -> list[dict]:
    """Find statistically meaningful file-level logical coupling in git history."""
    repo_root = _git_repo_root(directory, timeout) or directory.resolve()
    try:
        pathspec = directory.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return []
    pathspec = pathspec or "."
    try:
        result = subprocess.run(
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
        return []
    if result.returncode != 0 or not result.stdout:
        return []

    commits, preferred = _parse_git_name_status(result.stdout)
    normalized = [
        identities
        for identities in commits
        if 1 <= len(identities) <= max_commit_files
    ]
    if not normalized:
        return []

    file_commits = Counter(identity for files in normalized for identity in files)
    shared = Counter()
    for files in normalized:
        ordered = sorted(files)
        for index, source in enumerate(ordered):
            for target in ordered[index + 1:]:
                shared[(source, target)] += 1

    total = len(normalized)
    edges = []
    for (source, target), together in shared.items():
        if together < min_shared_commits:
            continue
        source_count = file_commits[source]
        target_count = file_commits[target]
        ochiai = together / math.sqrt(source_count * target_count)
        support = together / total
        lift = together * total / (source_count * target_count)
        denominator = -math.log(support)
        npmi = math.log(lift) / denominator if denominator else 0.0
        if ochiai < min_ochiai or lift <= 1 or npmi <= 0:
            continue
        edges.append({
            "source": preferred.get(source, source),
            "target": preferred.get(target, target),
            "type": CO_CHANGE_EDGE,
            "weight": round(ochiai, 6),
            "ochiai": round(ochiai, 6),
            "npmi": round(npmi, 6),
            "lift": round(lift, 6),
            "support": round(support, 6),
            "shared_commits": together,
        })
    return sorted(edges, key=lambda edge: (-edge["weight"], edge["source"], edge["target"]))


def _git_repo_root(directory: Path, timeout: float) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(directory),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout or b"\0" in result.stdout:
        return None
    root = Path(result.stdout.decode("utf-8", errors="surrogateescape").strip())
    return root.resolve() if root.is_absolute() else None


def _parse_git_name_status(data: bytes) -> tuple[list[set[int]], dict[int, str]]:
    fields = [field.decode("utf-8", errors="surrogateescape") for field in data.split(b"\0")]
    records: list[list[tuple[str, ...]]] = []
    current: list[tuple[str, ...]] | None = None
    index = 0
    while index < len(fields):
        field = fields[index]
        record = field.lstrip("\r\n")
        if record == "COMMIT" and index + 1 < len(fields):
            current = []
            records.append(current)
            index += 2
            continue
        if not field or current is None:
            index += 1
            continue
        status = record
        if status.startswith(("R", "C")) and index + 2 < len(fields):
            old_path, new_path = fields[index + 1:index + 3]
            current.append((status, _normalize_git_path(old_path), _normalize_git_path(new_path)))
            index += 3
            continue
        if index + 1 < len(fields):
            path = fields[index + 1]
            current.append((status, _normalize_git_path(path)))
            index += 2
            continue
        index += 1

    commits: list[set[int]] = []
    active: dict[str, int] = {}
    preferred: dict[int, str] = {}
    next_identity = 0

    def new_identity(path: str) -> int:
        nonlocal next_identity
        identity = next_identity
        next_identity += 1
        active[path] = identity
        preferred[identity] = path
        return identity

    for changes in records:
        identities = set()
        for change in changes:
            status = change[0]
            if status.startswith("R"):
                old_path, new_path = change[1:]
                identity = active.pop(old_path, None)
                if identity is None:
                    identity = new_identity(new_path)
                else:
                    active[new_path] = identity
                    preferred[identity] = new_path
                identities.add(identity)
            elif status.startswith("C"):
                new_path = change[2]
                identities.add(new_identity(new_path))
            else:
                path = change[1]
                identity = active.get(path)
                if identity is None:
                    identity = new_identity(path)
                identities.add(identity)
                if status.startswith("D"):
                    active.pop(path, None)
        commits.append(identities)
    return commits, preferred


def _normalize_git_path(path: str) -> str:
    return path.replace("\\", "/")


def refine_call_edges_with_co_changes(
    call_edges: list[dict], co_change_edges: list[dict], directory: Path | None = None
) -> list[dict]:
    """Add co-change evidence to confirmed calls without changing edge semantics."""
    root = (_git_repo_root(directory, 10) or directory.resolve()) if directory else None
    coupling = {
        frozenset((
            _normalize_edge_path(edge["source"], root),
            _normalize_edge_path(edge["target"], root),
        )): edge
        for edge in co_change_edges
        if edge.get("type") == CO_CHANGE_EDGE
    }
    refined = []
    for edge in call_edges:
        updated = dict(edge)
        match = coupling.get(frozenset((
            _normalize_edge_path(edge.get("source", ""), root),
            _normalize_edge_path(edge.get("target", ""), root),
        )))
        if (
            edge.get("type") == "CALLS"
            and edge.get("confidence") == "confirmed"
            and match
        ):
            evidence = dict(edge.get("evidence", {}))
            evidence["co_change_weight"] = match["weight"]
            updated["evidence"] = evidence
        refined.append(updated)
    return refined


def _normalize_edge_path(path: str, root: Path | None) -> str:
    normalized = _normalize_git_path(path)
    candidate = Path(normalized)
    if root and candidate.is_absolute():
        try:
            normalized = candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            pass
    return _normalize_git_path(normalized)


def _get_git_info(file_path: Path) -> dict:
    """Get git commit info for a file (bi-temporal tracking).

    Returns dict with commit_hash, commit_date, author.
    Falls back to empty strings if not in a git repo.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H|%cI|%an", "--", str(file_path)],
            capture_output=True, text=True, timeout=5,
            cwd=str(file_path.parent) if file_path.parent.exists() else None,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|")
            return {
                "commit_hash": parts[0] if len(parts) > 0 else "",
                "commit_date": parts[1] if len(parts) > 1 else "",
                "author": parts[2] if len(parts) > 2 else "",
            }
    except Exception:
        pass
    return {"commit_hash": "", "commit_date": "", "author": ""}


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


def _parse_file(file_path: Path, registry: SymbolRegistry, workspace_root: Path) -> dict:
    lang = detect_language(file_path)
    if not lang:
        return {"file": str(file_path), "language": None, "functions": [],
                "classes": [], "calls": [], "imports": []}

    parser = _get_parser(lang)
    if parser is None:
        # Fallback: regex-based extraction (less accurate but no deps).
        return _regex_parse(file_path, lang, registry, workspace_root)

    source = file_path.read_bytes()
    tree = parser.parse(source)

    extracted = _extract_symbols(tree, parser.language, lang, source)
    if extracted is None:
        return _regex_parse(file_path, lang, registry, workspace_root)
    functions, classes, calls, imports = extracted
    if lang == "python":
        imports, calls = resolve_python_imports_and_calls(file_path, registry, workspace_root)
        calls = enrich_python_semantics(file_path, calls, workspace_root)

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


def _extract_symbols(tree, language, lang: str, source: bytes) -> tuple | None:
    """Execute the language query and return functions, classes, calls, imports."""
    try:
        import tree_sitter as ts

        query_source = (QUERY_DIR / f"{lang}.scm").read_text(encoding="utf-8")
        matches = ts.QueryCursor(ts.Query(language, query_source)).matches(tree.root_node)
    except (OSError, UnicodeError, Exception):
        return None

    groups = {name: [] for name in ("function", "class", "call", "import")}
    seen = {name: set() for name in groups}
    for _, captures in matches:
        for kind in groups:
            nodes = captures.get(f"{kind}.name", [])
            owners = captures.get(f"{kind}.node", nodes)
            if not isinstance(nodes, list):
                nodes = [nodes]
            if not isinstance(owners, list):
                owners = [owners]
            for index, node in enumerate(nodes):
                owner = owners[min(index, len(owners) - 1)] if owners else node
                key = (node.start_byte, node.end_byte, kind)
                if key in seen[kind]:
                    continue
                seen[kind].add(key)
                text = source[node.start_byte:node.end_byte].decode(
                    "utf-8", errors="ignore"
                )
                if kind == "import" and len(text) >= 2 and (
                    text[0] == text[-1] and text[0] in {"'", '"'}
                    or text[0] == "<" and text[-1] == ">"
                ):
                    text = text[1:-1]
                groups[kind].append({
                    "name": text,
                    "line": owner.start_point[0] + 1,
                    "end_line": owner.end_point[0] + 1,
                    "column": owner.start_point[1],
                    "end_column": owner.end_point[1],
                    **(
                        {"signature": signature}
                        if kind == "function"
                        and (
                            signature := _declaration_signature(
                                source[owner.start_byte:owner.end_byte].decode(
                                    "utf-8", errors="ignore"
                                ),
                                text,
                            )
                        )
                        else {}
                    ),
                })
    return tuple(groups[name] for name in ("function", "class", "call", "import"))


def _regex_parse(
    file_path: Path, lang: str, registry: SymbolRegistry, workspace_root: Path
) -> dict:
    """Fallback: regex-based symbol extraction (no tree-sitter required)."""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    functions = []
    classes = []
    calls = []
    imports = []

    if lang == "python":
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(r"\s*def\s+(\w+)", line)
            if m:
                functions.append(_regex_function(m.group(1), line, i))
            m = re.match(r"\s*class\s+(\w+)", line)
            if m:
                classes.append({"name": m.group(1), "line": i, "end_line": i})
            m = re.match(r"\s*(\w+)\s*\(", line)
            if m and m.group(1) not in {"if", "for", "while", "def", "class", "print"}:
                calls.append({"name": m.group(1), "line": i})
            m = re.match(r"\s*(?:from\s+\S+\s+)?import\s+(\w+)", line)
            if m:
                imports.append({"name": m.group(1), "line": i})
        imports, calls = resolve_python_imports_and_calls(file_path, registry, workspace_root)
        calls = enrich_python_semantics(file_path, calls, workspace_root)
    elif lang in ("javascript", "typescript"):
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(r"\s*(?:export\s+)?function\s+(\w+)", line)
            if m:
                functions.append(_regex_function(m.group(1), line, i))
            m = re.match(r"\s*(?:export\s+)?class\s+(\w+)", line)
            if m:
                classes.append({"name": m.group(1), "line": i, "end_line": i})
            m = re.match(r"\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", line)
            if m:
                functions.append(_regex_function(m.group(1), line, i))
            declared = {
                match.group(1)
                for match in [
                    re.match(r"\s*(?:export\s+)?function\s+(\w+)", line),
                    re.match(r"\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::[^{}]+)?\{", line),
                ]
                if match
            }
            for call in re.finditer(r"\b(\w+)\s*\(", line):
                name = call.group(1)
                if name not in declared and name not in {"if", "for", "while", "switch", "catch"}:
                    calls.append({"name": name, "line": i})
    else:
        _regex_parse_additional_languages(
            content, lang, functions, classes, calls, imports
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


def _regex_parse_additional_languages(
    content: str,
    lang: str,
    functions: list[dict],
    classes: list[dict],
    calls: list[dict],
    imports: list[dict],
) -> None:
    """Extract basic symbols for optional grammars when tree-sitter is absent."""
    patterns = {
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
    function_pattern, class_pattern = patterns[lang]
    declared = set()
    for line_number, line in enumerate(content.splitlines(), 1):
        function_match = re.search(function_pattern, line)
        if function_match:
            name = function_match.group(1)
            declared.add(name)
            functions.append(_regex_function(name, line, line_number))
        if class_pattern and (class_match := re.search(class_pattern, line)):
            classes.append({
                "name": class_match.group(1), "line": line_number, "end_line": line_number,
            })
        _regex_add_import(line, line_number, lang, imports)
        for call in re.finditer(r"\b(?:\w+(?:::|\.|->))?(\w+[!?]?)\s*[!(]", line):
            name = call.group(1)
            if lang == "rust":
                name = name.removesuffix("!")
            if name not in declared and name not in {
                "if", "for", "while", "switch", "catch", "class", "struct", "interface",
            }:
                calls.append({"name": name, "line": line_number})
        if lang in {"ruby", "bash"}:
            for command in re.finditer(r"(?:^|[;{])\s*([A-Za-z_]\w*[!?]?)\s+", line):
                name = command.group(1)
                if name not in declared and name not in {
                    "class", "def", "do", "else", "elsif", "end", "fi", "function",
                    "if", "load", "module", "require", "source", "then",
                }:
                    calls.append({"name": name, "line": line_number})


def _regex_function(name: str, declaration: str, line: int) -> dict:
    function = {"name": name, "line": line, "end_line": line}
    signature = _declaration_signature(declaration, name)
    if signature:
        function["signature"] = signature
    return function


def _declaration_signature(declaration: str, name: str) -> str | None:
    """Return a compact name-and-parameters signature when it is explicit."""
    match = re.search(rf"\b{re.escape(name)}\s*\(", declaration)
    if not match:
        return None
    start = declaration.find("(", match.start())
    depth = 0
    for index in range(start, len(declaration)):
        char = declaration[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                parameters = re.sub(r"\s+", " ", declaration[start:index + 1]).strip()
                parameters = re.sub(r"\s*,\s*", ", ", parameters)
                return f"{name}{parameters}"
    return None


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


def index_directory(directory: Path, verbose: bool = True) -> dict:
    """Index all source files in a directory.

    Returns stats: {files, functions, classes, calls, imports}
    """
    detect_code_tools(directory)
    if not directory.exists():
        return {"files": 0, "functions": 0, "classes": 0, "calls": 0, "imports": 0}

    stats = {"files": 0, "functions": 0, "classes": 0, "calls": 0, "imports": 0}
    extensions = set(LANGUAGE_MAP.keys())
    registry = build_python_symbol_registry(directory)

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        if any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv", "venv"}):
            continue

        result = _parse_file(path, registry, directory)
        if result["language"]:
            stats["files"] += 1
            stats["functions"] += len(result["functions"])
            stats["classes"] += len(result["classes"])
            stats["calls"] += len(result["calls"])
            stats["imports"] += len(result["imports"])

    if verbose:
        ts_status = "tree-sitter" if _have_tree_sitter() else "regex fallback"
        print(f"Indexed {stats['files']} files ({ts_status}):")
        print(f"  Functions: {stats['functions']}")
        print(f"  Classes:   {stats['classes']}")
        print(f"  Calls:     {stats['calls']}")
        print(f"  Imports:   {stats['imports']}")

    return stats


def _generation_catalog(directory: Path):
    """Open the shared generation catalog only when it already exists."""
    try:
        from .generation_catalog import GenerationCatalog
    except ImportError:
        from generation_catalog import GenerationCatalog

    state_root = Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(directory.resolve())))
    catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
    if not catalog_path.is_file():
        return None
    return GenerationCatalog(state_root, catalog_path=catalog_path)


def _active_evidence_graph(directory: Path):
    try:
        from .evidence_graph import EvidenceGraph
    except ImportError:
        from evidence_graph import EvidenceGraph

    try:
        catalog = _generation_catalog(directory)
        return None if catalog is None else EvidenceGraph.open_active(catalog)
    except (OSError, TypeError, ValueError, PermissionError, sqlite3.Error):
        return None


def _stored_location(graph, node_id: str, directory: Path) -> tuple[str, int]:
    occurrences = graph.occurrences(node_id, max_rows=1)
    if not occurrences:
        node = graph.node(node_id)
        path = "" if node is None else str(node["metadata"].get("path", ""))
        return (str(directory / path) if path else "", 0)
    occurrence = occurrences[0]
    return str(directory / occurrence["relative_path"]), occurrence["line_start"]


def _stored_edge_location(graph, assertion_id: str, directory: Path) -> tuple[str, int]:
    evidence = graph.evidence(assertion_id=assertion_id, max_rows=1)
    if not evidence:
        return "", 0
    span = evidence[0]
    return str(directory / span["relative_path"]), span["line_start"]


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


def _stored_communities(graph_reader, edges: list[dict[str, object]]) -> list[list[str]]:
    cache = getattr(graph_reader, "_derived_code_graph_cache", None)
    if cache is None:
        cache = {}
        graph_reader._derived_code_graph_cache = cache
    cache_key = "communities/calls/v1"
    if cache_key in cache:
        return cache[cache_key]
    graph: dict[str, dict[str, float]] = {}
    for edge in edges:
        source = str(edge["source_node_id"])
        target = str(edge["target_node_id"])
        if source == target:
            continue
        graph.setdefault(source, {})[target] = graph.setdefault(source, {}).get(target, 0) + 1
        graph.setdefault(target, {})[source] = graph.setdefault(target, {}).get(source, 0) + 1
    communities = _louvain_communities(graph)
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
        stored = (
            _store_find_callers(function_name, directory, with_report=True)
            if with_report
            else _store_find_callers(function_name, directory)
        )
        if stored is not None:
            return stored
    callers = []
    extensions = set(LANGUAGE_MAP.keys())
    registry = build_python_symbol_registry(directory)

    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv"}):
            continue

        result = _parse_file(path, registry, directory)
        for call in result["calls"]:
            resolved_name = (call.get("qualified_name") or call["name"]).rsplit(".", 1)[-1]
            is_python = result["language"] == "python"
            if resolved_name == function_name and (
                call.get("confidence") == "confirmed" or not is_python
            ):
                callers.append({
                    "file": str(path),
                    "line": call["line"],
                    "function": function_name,
                    "qualified_name": call.get("qualified_name"),
                    "confidence": call.get("confidence", "heuristic"),
                })

    return _with_report("callers", callers, _live_report(directory), with_report)


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
        target_ids = {item["node_id"] for item in targets}
        edges = graph.edges(edge_types=("CALLS",), max_rows=10_000)
        results = []
        for edge in edges:
            if edge["target_node_id"] not in target_ids:
                continue
            caller = graph.node(edge["source_node_id"])
            if caller is None:
                continue
            location = _stored_edge_location(graph, edge["assertion_id"], directory)
            results.append({
                "file": location[0],
                "line": location[1],
                "function": function_name,
                "qualified_name": _stored_qualified_name(caller),
                "confidence": edge["confidence"],
                "symbol_id": caller["node_id"],
            })
        results = sorted(results, key=lambda item: (item["file"], item["line"], item["symbol_id"]))
        return _with_report("callers", results, _store_report(graph), with_report)
    finally:
        graph.close()


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
        stored = (
            _store_find_callees(function_name, directory, with_report=True)
            if with_report
            else _store_find_callees(function_name, directory)
        )
        if stored is not None:
            return stored
    callees = []
    extensions = set(LANGUAGE_MAP.keys())
    registry = build_python_symbol_registry(directory)

    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv"}):
            continue

        result = _parse_file(path, registry, directory)
        # Find the function definition.
        in_function = False
        for func in result["functions"]:
            if func["name"] == function_name:
                in_function = True
                break

        if not in_function:
            continue

        # Find calls within the function's line range.
        func_def = next((f for f in result["functions"] if f["name"] == function_name), None)
        if not func_def:
            continue

        for call in result["calls"]:
            if func_def["line"] <= call["line"] <= func_def.get("end_line", call["line"]):
                callees.append({
                    "file": str(path),
                    "line": call["line"],
                    "callee": call["name"],
                })

    return _with_report("callees", callees, _live_report(directory), with_report)


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
        source_ids = {item["node_id"] for item in sources}
        results = []
        for edge in graph.edges(edge_types=("CALLS",), max_rows=10_000):
            if edge["source_node_id"] not in source_ids:
                continue
            callee = graph.node(edge["target_node_id"])
            if callee is None:
                continue
            location = _stored_edge_location(graph, edge["assertion_id"], directory)
            results.append({
                "file": location[0],
                "line": location[1],
                "callee": callee["metadata"].get("name", callee["identity_key"]),
                "symbol_id": callee["node_id"],
                "confidence": edge["confidence"],
            })
        results = sorted(results, key=lambda item: (str(item["callee"]), item["symbol_id"]))
        return _with_report("callees", results, _store_report(graph), with_report)
    finally:
        graph.close()


def find_dead_code(
    directory: Path, *, live: bool = False, with_report: bool = False
) -> list[dict] | dict:
    """Return conservative dead-code candidates from the incomplete static graph."""
    if not live:
        stored = (
            _store_find_dead_code(directory, with_report=True)
            if with_report
            else _store_find_dead_code(directory)
        )
        if stored is not None:
            return stored
    parsed, definitions, edges = _workspace_call_graph(directory)
    incoming = {edge["target"] for edge in edges}

    candidates = []
    for path, result in parsed:
        source = path.read_text(encoding="utf-8", errors="ignore")
        exports = _declared_exports(path, source, result["language"])
        lines = source.splitlines()
        for function in result["functions"]:
            name = function["name"]
            if (
                function["symbol_id"] in incoming
                or name in {"main", "__init__"}
                or name.startswith("test_")
                or path.name.startswith("test_")
                or name in exports
                or _is_framework_route(lines, function["line"])
            ):
                continue
            candidates.append({
                "name": name,
                "symbol_id": function["symbol_id"],
                "owner": function["owner"],
                "file": str(path),
                "line": function["line"],
                "status": "candidate",
                "reason": "zero_confirmed_incoming_calls",
                "graph_complete": False,
            })
    candidates = sorted(candidates, key=lambda item: (item["name"], item["file"], item["line"]))
    return _with_report(
        "candidates", candidates, _live_report(directory, parsed), with_report
    )


def _store_find_dead_code(
    directory: Path, *, with_report: bool = False
) -> list[dict] | dict | None:
    graph = _active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        nodes = graph.find_nodes(kinds=("function", "method"), max_rows=10_000)
        call_edges = graph.edges(edge_types=("CALLS",), max_rows=10_000)
        exposed = {
            edge["source_node_id"]
            for edge in graph.edges(edge_types=("EXPOSES",), max_rows=10_000)
        }
        incoming = {edge["target_node_id"] for edge in call_edges}
        candidates = []
        for node in nodes:
            name = str(node["metadata"].get("name", ""))
            path = str(node["metadata"].get("path", ""))
            if (
                node["node_id"] in incoming
                or node["node_id"] in exposed
                or name in {"main", "__init__"}
                or name.startswith("test_")
                or PurePath(path).name.startswith("test_")
            ):
                continue
            location = _stored_location(graph, node["node_id"], directory)
            candidates.append({
                "name": name,
                "symbol_id": node["node_id"],
                "owner": node["metadata"].get("owner", ""),
                "file": location[0],
                "line": location[1],
                "status": "candidate",
                "reason": "zero_confirmed_incoming_calls",
                "graph_complete": False,
            })
        report = _store_report(graph)
        for candidate in candidates:
            candidate["graph_complete"] = report["graph_complete"]
        candidates = sorted(candidates, key=lambda item: (item["name"], item["file"], item["line"]))
        return _with_report("candidates", candidates, report, with_report)
    finally:
        graph.close()


def _declared_exports(path: Path, source: str, language: str) -> set[str]:
    if language == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return set()
                return {item for item in value if isinstance(item, str)}
        return set()
    if language in {"javascript", "typescript"}:
        return set(re.findall(r"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function|class)\s+(\w+)", source))
    return set()


def _is_framework_route(lines: list[str], definition_line: int) -> bool:
    prefix = "\n".join(lines[max(0, definition_line - 2):definition_line])
    return bool(re.search(r"(?:@\w*\.route\s*\(|@(?:GetMapping|RequestMapping)\b)", prefix))


def get_architecture(
    directory: Path, *, live: bool = False, with_report: bool = False
) -> dict:
    """Summarize statically visible entry points, routes, hotspots, and modules."""
    if not live:
        stored = _store_get_architecture(directory)
        if stored is not None:
            return stored
    parsed, definitions, edges = _workspace_call_graph(directory)
    entry_points = []
    routes = []
    incoming: dict[str, set[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["target"], set()).add(edge["source"])
    for path, result in parsed:
        source = path.read_text(encoding="utf-8", errors="ignore")
        for function in result["functions"]:
            if function["name"] == "main":
                entry_points.append({
                    "kind": "main", "name": "main", "file": str(path),
                    "line": function["line"],
                })
        entry_points.extend(_listen_entry_points(path, source))
        routes.extend(_framework_routes(path, source))

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
    hotspots.sort(key=lambda item: (-item["incoming_callers"], item["name"], item["file"]))
    architecture = {
        "entry_points": entry_points,
        "routes": routes,
        "hotspots": hotspots,
        "communities": _communities_from_edges(edges),
        "graph_complete": False,
    }
    return {**architecture, **_live_report(directory, parsed)}


def _store_get_architecture(directory: Path) -> dict | None:
    graph = _active_evidence_graph(directory)
    if graph is None:
        return None
    try:
        entries = graph.find_nodes(kinds=("entry-point",), max_rows=10_000)
        routes = graph.find_nodes(kinds=("route",), max_rows=10_000)
        functions = {
            node["node_id"]: node
            for node in graph.find_nodes(kinds=("function", "method"), max_rows=10_000)
        }
        calls = graph.edges(edge_types=("CALLS",), max_rows=10_000)
        incoming: dict[str, set[str]] = {}
        for edge in calls:
            incoming.setdefault(edge["target_node_id"], set()).add(
                edge["source_node_id"]
            )
        hotspots = []
        for node_id, callers in incoming.items():
            node = functions.get(node_id)
            if node is None:
                continue
            location = _stored_location(graph, node_id, directory)
            hotspots.append({
                "name": node["metadata"].get("name", node["identity_key"]),
                "symbol_id": node_id,
                "owner": node["metadata"].get("owner", ""),
                "file": location[0],
                "line": location[1],
                "incoming_callers": len(callers),
            })
        hotspots.sort(key=lambda item: (-item["incoming_callers"], str(item["name"]), item["file"]))
        report = _store_report(graph)
        return {
            "entry_points": [_stored_architecture_node(graph, node, directory) for node in entries],
            "routes": [_stored_architecture_node(graph, node, directory) for node in routes],
            "hotspots": hotspots,
            "communities": _stored_communities(graph, calls),
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
        communities = _stored_communities(
            graph, graph.edges(edge_types=("CALLS",), max_rows=10_000)
        )
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


def _find_live_dependencies(
    node_id: str,
    definitions: dict[str, dict],
    edges: list[dict],
    *,
    reverse: bool,
) -> list[dict]:
    start_key, target_key = (
        ("target", "source") if reverse else ("source", "target")
    )
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge[start_key], []).append(edge[target_key])
    pending = [(identifier, 0) for identifier in _live_node_ids(node_id, definitions)]
    seen = {identifier for identifier, _depth in pending}
    results = []
    work = 0
    while pending and len(results) < 10_000 and work < 10_000:
        current, depth = pending.pop(0)
        work += 1
        if depth >= 8:
            continue
        for target in sorted(adjacency.get(current, [])):
            if target in seen:
                continue
            seen.add(target)
            target_depth = depth + 1
            definition = definitions.get(target)
            if definition is not None:
                results.append(
                    {
                        "node_id": target,
                        "kind": "function",
                        "identity_scheme": "live-code/v1",
                        "identity_key": target,
                        "metadata": {
                            "name": definition["name"],
                            "owner": definition.get("owner", ""),
                            "path": definition.get("file", ""),
                        },
                        "depth": target_depth,
                    }
                )
            pending.append((target, target_depth))
    return results


def _find_live_paths(
    source_node_id: str,
    target_node_id: str,
    definitions: dict[str, dict],
    edges: list[dict],
) -> list[dict]:
    sources = _live_node_ids(source_node_id, definitions)
    targets = set(_live_node_ids(target_node_id, definitions))
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge["target"])
    pending = [(source, [source]) for source in sorted(sources)]
    paths = []
    work = 0
    while pending and len(paths) < 10 and work < 10_000:
        node, path = pending.pop(0)
        work += 1
        if node in targets and len(path) > 1:
            paths.append({"node_ids": path, "assertion_ids": [], "depth": len(path) - 1})
            continue
        if len(path) > 8:
            continue
        for target in sorted(outgoing.get(node, [])):
            if target not in path:
                pending.append((target, [*path, target]))
    return paths


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
        if (
            not path.is_file()
            or path.suffix.lower() not in LANGUAGE_MAP
            or any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv", "venv"})
        ):
            continue
        result = _parse_file(path, registry, directory)
        _annotate_function_ids(path, result, directory)
        parsed.append((path, result))
        for function in result["functions"]:
            definition = {**function, "file": str(path), "language": result["language"]}
            definitions[function["symbol_id"]] = definition
            by_name.setdefault(function["name"], []).append(definition)
            if result["language"] == "python":
                by_qualified[_python_qualified_name(path, function, directory)] = definition

    edges = []
    for path, result in parsed:
        for call in result["calls"]:
            caller = _containing_function(result["functions"], call)
            if caller is None:
                continue
            target = None
            if result["language"] == "python":
                if call.get("confidence") != "confirmed":
                    continue
                target = by_qualified.get(call.get("qualified_name"))
            same_file = [
                item for item in by_name.get(call["name"], []) if item["file"] == str(path)
            ]
            if target is None and len(same_file) == 1:
                target = same_file[0]
            if target is None and len(by_name.get(call["name"], [])) == 1:
                target = by_name[call["name"]][0]
            if target is not None:
                edges.append({"source": caller["symbol_id"], "target": target["symbol_id"]})
    return parsed, definitions, edges


def _annotate_function_ids(path: Path, result: dict, root: Path) -> None:
    containers = [*result["classes"], *result["functions"]]
    relative = path.resolve().relative_to(root).as_posix()
    annotated = []
    for function in result["functions"]:
        enclosing = [
            item for item in containers
            if item is not function and _symbol_contains(item, function)
        ]
        owner = min(enclosing, key=_symbol_span)["name"] if enclosing else "<module>"
        function["owner"] = owner
        identity = function.get("signature") or f"{function['name']}@L{function['line']}"
        annotated.append((function, f"{relative}::{owner}::{identity}"))
    counts: dict[str, int] = {}
    for _, identity in annotated:
        counts[identity] = counts.get(identity, 0) + 1
    for function, identity in annotated:
        function["symbol_id"] = (
            f"{identity}@L{function['line']}" if counts[identity] > 1 else identity
        )


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


def _louvain_communities(graph: dict[str, dict[str, float]]) -> list[list[str]]:
    """Optimize modularity on a weighted undirected graph without dependencies."""
    current = {node: dict(neighbors) for node, neighbors in graph.items()}
    members = {node: {node} for node in current}
    while current:
        nodes = sorted(current, key=str)
        community = {node: node for node in nodes}
        degree = {node: sum(current[node].values()) for node in nodes}
        totals = dict(degree)
        m2 = sum(degree.values())
        if not m2:
            break

        moved = True
        while moved:
            moved = False
            for node in nodes:
                old = community[node]
                totals[old] -= degree[node]
                weights: dict[object, float] = {}
                for neighbor, weight in current[node].items():
                    target = community[neighbor]
                    weights[target] = weights.get(target, 0.0) + weight
                choices = sorted(weights, key=str)
                best = old
                best_gain = 0.0
                for target in choices:
                    gain = weights[target] - totals[target] * degree[node] / m2
                    if gain > best_gain + 1e-12:
                        best, best_gain = target, gain
                community[node] = best
                totals[best] += degree[node]
                moved |= best != old

        groups: dict[object, list[object]] = {}
        for node in nodes:
            groups.setdefault(community[node], []).append(node)
        ordered_groups = sorted(
            (sorted(group, key=str) for group in groups.values()),
            key=lambda group: str(group[0]),
        )
        if len(ordered_groups) == len(nodes):
            break

        group_of = {
            node: index for index, group in enumerate(ordered_groups) for node in group
        }
        next_members = {
            index: set().union(*(members[node] for node in group))
            for index, group in enumerate(ordered_groups)
        }
        reduced = _aggregate_louvain_graph(current, group_of)
        current, members = reduced, next_members

    communities = [sorted(group) for group in members.values() if len(group) >= 2]
    return sorted(communities, key=lambda group: group[0])


def _aggregate_louvain_graph(
    graph: dict[object, dict[object, float]], group_of: dict[object, int]
) -> dict[int, dict[int, float]]:
    """Aggregate an undirected weighted graph using adjacency degree weights."""
    reduced: dict[int, dict[int, float]] = {
        group: {} for group in sorted(set(group_of.values()))
    }
    for source in sorted(graph, key=str):
        for target, weight in graph[source].items():
            left, right = group_of[source], group_of[target]
            if source == target:
                reduced[left][left] = reduced[left].get(left, 0.0) + weight
            elif str(source) <= str(target):
                if left == right:
                    reduced[left][left] = reduced[left].get(left, 0.0) + 2 * weight
                else:
                    reduced[left][right] = reduced[left].get(right, 0.0) + weight
                    reduced[right][left] = reduced[right].get(left, 0.0) + weight
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
