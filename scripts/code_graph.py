"""Code graph — tree-sitter based code intelligence for the vault.

Parses source code files into a knowledge graph: functions, classes,
imports, call edges. Stores results for graph queries and impact analysis.

The graph enables:
- "Who calls this function?" — caller analysis
- "What will break if I change this?" — impact analysis (blast radius)
- "Find dead code" — functions with zero callers

Languages (3-tier model, like repowise):
- Full: Python, TypeScript, JavaScript (tree-sitter grammars)
- Good: (future — Go, Rust, C)
- File-only: (future — syntax-only for other languages)

Install: uv sync --extra code-graph
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Lazy-loaded tree-sitter.
_ts: dict = {}  # language_name → (Language, Parser)

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
}

# Query patterns for symbol extraction.
# These are simplified — full .scm files would be loaded from scripts/queries/.
FUNCTION_QUERY = {
    "python": "(function_definition name: (identifier) @name) @func",
    "javascript": "(function_declaration name: (identifier) @name) @func",
    "typescript": "(function_declaration name: (identifier) @name) @func",
}

CLASS_QUERY = {
    "python": "(class_definition name: (identifier) @name) @cls",
    "javascript": "(class_declaration name: (identifier) @name) @cls",
    "typescript": "(class_declaration name: (identifier) @name) @cls",
}

CALL_QUERY = {
    "python": "(call function: (identifier) @name) @call",
    "javascript": "(call_expression function: (identifier) @name) @call",
    "typescript": "(call_expression function: (identifier) @name) @call",
}

IMPORT_QUERY = {
    "python": "(import_statement (dotted_name (identifier) @name)) @import",
    "javascript": "(import_statement (identifier) @name) @import",
    "typescript": "(import_statement (identifier) @name) @import",
}


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
        from tree_sitter import Parser

        if lang == "python":
            import tree_sitter_python
            language = ts.Language(tree_sitter_python.language())
        elif lang == "javascript":
            import tree_sitter_javascript
            language = ts.Language(tree_sitter_javascript.language())
        elif lang == "typescript":
            import tree_sitter_typescript
            language = ts.Language(tree_sitter_typescript.language_typescript())
        else:
            return None

        parser = Parser(language)
        _ts[lang] = parser
        return parser
    except ImportError:
        return None
    except Exception:
        return None


def detect_language(file_path: Path) -> str | None:
    """Detect language from file extension."""
    return LANGUAGE_MAP.get(file_path.suffix.lower())


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
    lang = detect_language(file_path)
    if not lang:
        return {"file": str(file_path), "language": None, "functions": [],
                "classes": [], "calls": [], "imports": []}

    parser = _get_parser(lang)
    if parser is None:
        # Fallback: regex-based extraction (less accurate but no deps).
        return _regex_parse(file_path, lang)

    source = file_path.read_bytes()
    tree = parser.parse(source)

    functions = _extract_symbols(tree, lang, FUNCTION_QUERY, source)
    classes = _extract_symbols(tree, lang, CLASS_QUERY, source)
    calls = _extract_symbols(tree, lang, CALL_QUERY, source)
    imports = _extract_symbols(tree, lang, IMPORT_QUERY, source)

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


def _extract_symbols(tree, lang: str, query_map: dict, source: bytes) -> list[dict]:
    """Extract symbols matching a tree-sitter query."""
    query_str = query_map.get(lang)
    if not query_str:
        return []

    try:
        # Walk the tree manually for symbol extraction.
        # Full query support requires tree-sitter query API.
        return _walk_tree_for_symbols(tree.root_node, query_str, source)
    except Exception:
        return []


def _walk_tree_for_symbols(node, query_type: str, source: bytes) -> list[dict]:
    """Walk the AST and extract symbols based on query type patterns."""
    results = []

    # Determine what node types to look for based on query.
    if "function_definition" in query_type or "function_declaration" in query_type:
        target_types = {"function_definition", "function_declaration"}
        name_field = "name"
    elif "class_definition" in query_type or "class_declaration" in query_type:
        target_types = {"class_definition", "class_declaration"}
        name_field = "name"
    elif "call" in query_type.lower():
        target_types = {"call", "call_expression"}
        name_field = "function"
    elif "import" in query_type:
        target_types = {"import_statement"}
        name_field = None
    else:
        return []

    def _walk(n):
        if n.type in target_types:
            name = None
            if name_field:
                child = n.child_by_field_name(name_field)
                if child:
                    name = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
            results.append({
                "name": name or "<anonymous>",
                "line": n.start_point[0] + 1,
                "end_line": n.end_point[0] + 1,
            })
        for child in n.children:
            _walk(child)

    _walk(node)
    return results


def _regex_parse(file_path: Path, lang: str) -> dict:
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
                functions.append({"name": m.group(1), "line": i, "end_line": i})
            m = re.match(r"\s*class\s+(\w+)", line)
            if m:
                classes.append({"name": m.group(1), "line": i, "end_line": i})
            m = re.match(r"\s*(\w+)\s*\(", line)
            if m and m.group(1) not in {"if", "for", "while", "def", "class", "print"}:
                calls.append({"name": m.group(1), "line": i})
            m = re.match(r"\s*(?:from\s+\S+\s+)?import\s+(\w+)", line)
            if m:
                imports.append({"name": m.group(1), "line": i})
    elif lang in ("javascript", "typescript"):
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(r"\s*(?:export\s+)?function\s+(\w+)", line)
            if m:
                functions.append({"name": m.group(1), "line": i, "end_line": i})
            m = re.match(r"\s*(?:export\s+)?class\s+(\w+)", line)
            if m:
                classes.append({"name": m.group(1), "line": i, "end_line": i})
            m = re.match(r"\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", line)
            if m:
                functions.append({"name": m.group(1), "line": i, "end_line": i})

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


def index_directory(directory: Path, verbose: bool = True) -> dict:
    """Index all source files in a directory.

    Returns stats: {files, functions, classes, calls, imports}
    """
    if not directory.exists():
        return {"files": 0, "functions": 0, "classes": 0, "calls": 0, "imports": 0}

    stats = {"files": 0, "functions": 0, "classes": 0, "calls": 0, "imports": 0}
    extensions = set(LANGUAGE_MAP.keys())

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        if any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv", "venv"}):
            continue

        result = parse_file(path)
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


def find_callers(function_name: str, directory: Path) -> list[dict]:
    """Find all files that call a function by name (CALLS edge, reverse direction).

    Returns list of {file, line, function}.
    """
    callers = []
    extensions = set(LANGUAGE_MAP.keys())

    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv"}):
            continue

        result = parse_file(path)
        for call in result["calls"]:
            if call["name"] == function_name:
                callers.append({
                    "file": str(path),
                    "line": call["line"],
                    "function": function_name,
                })

    return callers


def find_callees(function_name: str, directory: Path) -> list[dict]:
    """Find all functions called BY a function (CALLS edge, forward direction).

    Returns list of {file, line, callee}.
    """
    callees = []
    extensions = set(LANGUAGE_MAP.keys())

    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv"}):
            continue

        result = parse_file(path)
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

    return callees


def detect_communities(directory: Path) -> list[list[str]]:
    """Detect functional modules in code using label propagation.

    Pure Python, zero dependencies. Works on the call graph:
    functions that call each other frequently → same community.

    Returns list of communities, each a list of function names.
    """
    from collections import defaultdict

    # Build adjacency: function → set of functions it calls.
    adj: dict[str, set[str]] = defaultdict(set)
    extensions = set(LANGUAGE_MAP.keys())

    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(skip in path.parts for skip in {".git", "node_modules", "__pycache__", ".venv"}):
            continue

        result = parse_file(path)
        func_names = {f["name"] for f in result["functions"]}
        for call in result["calls"]:
            callee = call["name"]
            # Only track calls to functions defined in this codebase.
            for fn in func_names:
                adj[fn].add(callee)

    if not adj:
        return []

    # Label Propagation Algorithm (LPA).
    # Each node starts with a unique label. Iteratively, each node adopts
    # the most frequent label among its neighbors. Converges to communities.
    nodes = set()
    for src, targets in adj.items():
        nodes.add(src)
        nodes.update(targets)

    labels = {node: i for i, node in enumerate(sorted(nodes))}

    for _ in range(10):  # Max 10 iterations.
        changed = False
        for node in sorted(nodes):
            if node not in adj:
                continue
            neighbor_labels = [labels.get(t, labels[node]) for t in adj[node] if t in labels]
            if not neighbor_labels:
                continue
            # Most common label among neighbors.
            from collections import Counter
            most_common = Counter(neighbor_labels).most_common(1)[0][0]
            if most_common != labels[node]:
                labels[node] = most_common
                changed = True
        if not changed:
            break

    # Group nodes by final label.
    communities: dict[int, list[str]] = defaultdict(list)
    for node, label in labels.items():
        communities[label].append(node)

    # Return only communities with 2+ members.
    return [sorted(members) for members in communities.values() if len(members) >= 2]


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
