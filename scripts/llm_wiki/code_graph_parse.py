"""Parsing source files into symbols, imports and calls with tree-sitter or a regex fallback."""
from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

try:
    from ..code_languages import (
        CODE_LANGUAGE_BY_SUFFIX,
        language_for_path,
    )
except ImportError:
    from code_languages import (
        CODE_LANGUAGE_BY_SUFFIX,
        language_for_path,
    )
try:
    from ..import_resolver import (
        EMPTY_REGISTRY,
        SymbolRegistry,
        build_python_symbol_registry,
        directory_skipped,
        own_source_file,
        resolve_python_imports_and_calls,
    )
except ImportError:
    from import_resolver import (
        EMPTY_REGISTRY,
        SymbolRegistry,
        build_python_symbol_registry,
        directory_skipped,
        own_source_file,
        resolve_python_imports_and_calls,
    )

# Lazy-loaded tree-sitter parsers.
tree_sitter_parsers: dict = {}  # language_name -> Parser

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


QUERY_DIR = Path(__file__).parents[1] / "queries"


def _have_tree_sitter() -> bool:
    """Check if tree-sitter is importable."""
    try:
        import tree_sitter  # noqa: F401
        return True
    except ImportError:
        return False


# A fallback reason is one exception line. 200 characters is the bound retrieval
# gives the same kind of reason (`search_memory.EMBEDDER_REASON_MAX_CHARS`), so the
# two read alike; `describe_error` redacts it first.
FALLBACK_REASON_MAX_CHARS = 200


def _fallback_reason(error: Exception) -> str:
    """Why a parse stage fell back, as `Class: redacted message`."""
    from secret_redact import describe_error

    return describe_error(error)[:FALLBACK_REASON_MAX_CHARS]


def parser_attempt(lang: str) -> tuple:
    """(parser, None); (None, why) when loading failed; (None, None) for no grammar.

    The regex fallback used to be taken with the cause thrown away, so a missing
    grammar package and a broken one looked the same. See
    `docs/research/2026-09-28-a-check-names-its-cause.md`.
    """
    if lang in tree_sitter_parsers:
        return tree_sitter_parsers[lang], None
    try:
        return _new_parser(lang), None
    except Exception as exc:  # noqa: BLE001 - ImportError included: the fallback names it
        return None, _fallback_reason(exc)


def _new_parser(lang: str):
    """A cached parser for the language, or None when no grammar is configured."""
    loader = GRAMMAR_LOADERS.get(lang)
    if loader is None:
        return None
    import tree_sitter as ts

    module_name, factory_name = loader
    grammar = importlib.import_module(module_name)
    parser = ts.Parser(ts.Language(getattr(grammar, factory_name)()))
    tree_sitter_parsers[lang] = parser
    return parser


def detect_language(file_path: Path) -> str | None:
    """Detect language from file extension."""
    return language_for_path(file_path)


def _jedi_script(file_path: Path, workspace_root: Path) -> tuple:
    """(script, None); (None, why) when Jedi failed; (None, None) when it is absent.

    Jedi is an optional extra, so its absence is the documented state, not a failure.
    """
    try:
        jedi = importlib.import_module("jedi")
    except ImportError:
        return None, None
    try:
        project = jedi.Project(path=str(workspace_root.resolve()))
        return jedi.Script(path=str(file_path.resolve()), project=project), None
    except Exception as exc:  # noqa: BLE001 - the calls stay unresolved and say why
        return None, _fallback_reason(exc)


def _workspace_definition(definition, workspace: Path) -> tuple | None:
    """One inferred definition as a (full_name, path) pair inside the workspace."""
    module_path = getattr(definition, "module_path", None)
    full_name = getattr(definition, "full_name", None)
    if not module_path or not full_name:
        return None
    resolved_path = Path(module_path).resolve()
    resolved_path.relative_to(workspace)
    return (full_name, resolved_path)


def _inferred_targets(script, call: dict, workspace: Path) -> tuple:
    """(the distinct workspace targets Jedi infers for one call site, None), or (set(), why)."""
    try:
        definitions = script.infer(line=call["line"], column=call.get("column", 0))
        pairs = (_workspace_definition(item, workspace) for item in definitions)
        return {pair for pair in pairs if pair is not None}, None
    except Exception as exc:  # noqa: BLE001 - this call stays unresolved and says why
        return set(), _fallback_reason(exc)


def _semantically_resolvable(call: dict) -> bool:
    return call.get("confidence") == "unknown" and call.get("semantic_eligible", False)


def enriched_call(script, call: dict, workspace: Path) -> dict:
    """A call resolved to its one workspace target, or the call unchanged."""
    if not _semantically_resolvable(call):
        return call
    unique, reason = _inferred_targets(script, call, workspace)
    if reason is not None:
        return {**call, "semantic_error": reason}
    return _resolved_call(call, unique)


def _resolved_call(call: dict, unique: set) -> dict:
    """The call bound to its one inferred target, or unchanged when there is not one."""
    if len(unique) != 1:
        return call
    full_name, _ = unique.pop()
    updated = dict(call)
    updated.update(qualified_name=full_name, confidence="confirmed", evidence="jedi")
    return updated


def _unresolved_call(call: dict, reason: str) -> dict:
    """A call Jedi would have tried carries why it could not."""
    if not _semantically_resolvable(call):
        return call
    return {**call, "semantic_error": reason}


def enrich_python_semantics(file_path: Path, calls: list[dict], workspace_root: Path) -> list[dict]:
    """Resolve unknown Python calls with Jedi when it yields one workspace target."""
    script, reason = _jedi_script(file_path, workspace_root)
    if reason is not None:
        return [_unresolved_call(call, reason) for call in calls]
    if script is None:
        return calls
    workspace = workspace_root.resolve()
    return [enriched_call(script, call, workspace) for call in calls]


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
    return parse_file_in_workspace(file_path, registry, file_path.parent)


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


def _tree_sitter_symbols(file_path: Path, lang: str, registry, workspace_root) -> tuple:
    """(symbols, None), or (None, why or None) when the caller must fall back to regex."""
    parser, reason = parser_attempt(lang)
    if parser is None:
        return None, reason
    source = file_path.read_bytes()
    extracted, reason = _extract_symbols(parser.parse(source), parser.language, lang, source)
    if extracted is None:
        return None, reason
    return _language_symbols(file_path, lang, registry, workspace_root, extracted), None


def _regex_fallback(
    file_path: Path, lang: str, registry, workspace_root: Path, reason: str | None
) -> dict:
    """Regex-based extraction (less accurate, no deps), naming why tree-sitter was not used."""
    result = _regex_parse(file_path, lang, registry, workspace_root)
    if reason is not None:
        result["parser_fallback_reason"] = reason
    return result


def parse_file_in_workspace(file_path: Path, registry: SymbolRegistry, workspace_root: Path) -> dict:
    lang = detect_language(file_path)
    if not lang:
        return _empty_parse_result(file_path)
    symbols, reason = _tree_sitter_symbols(file_path, lang, registry, workspace_root)
    if symbols is None:
        return _regex_fallback(file_path, lang, registry, workspace_root, reason)
    functions, classes, calls, imports = symbols
    return {
        "file": str(file_path),
        "language": lang,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "imports": imports,
    }


def _query_matches(tree, language, lang: str) -> tuple:
    """(the query matches for this language, None), or (None, why it cannot run)."""
    try:
        import tree_sitter as ts

        query_source = (QUERY_DIR / f"{lang}.scm").read_text(encoding="utf-8")
        return ts.QueryCursor(ts.Query(language, query_source)).matches(tree.root_node), None
    except Exception as exc:  # noqa: BLE001 - the regex fallback runs and names this
        return None, _fallback_reason(exc)


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


def _extract_symbols(tree, language, lang: str, source: bytes) -> tuple:
    """((functions, classes, calls, imports), None), or (None, why the query failed)."""
    matches, reason = _query_matches(tree, language, lang)
    if matches is None:
        return None, reason
    groups = _collect_all_symbol_kinds(matches, source)
    return tuple(groups[name] for name in _SYMBOL_KINDS), None


_SCRIPT_CALL_KEYWORDS = {"if", "for", "while", "switch", "catch"}


def _regex_python_definitions(
    line: str, number: int, functions: list, classes: list
) -> None:
    match = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
    if match:
        functions.append(_regex_function(match.group(1), line, number))
    match = re.match(r"\s*class\s+(\w+)", line)
    if match:
        classes.append({"name": match.group(1), "line": number, "end_line": number})


def _regex_parse_python(
    content: str,
    file_path: Path,
    registry: SymbolRegistry,
    workspace_root: Path,
    functions: list,
    classes: list,
) -> tuple[list, list]:
    """Python calls and imports come from the resolver, not from the regex pass.

    The pass therefore collects definitions only; it used to match a call and an
    import pattern on every line and throw both lists away (audit 3, C7).
    """
    for number, line in enumerate(content.splitlines(), 1):
        _regex_python_definitions(line, number, functions, classes)
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


# Audit 3, B24. The regex fallback used to write `end_line = line`, so no call
# was ever inside a function: live `callees` answered `[]` and live `dead_code`
# named called functions as dead. A block now has an end: by indentation where
# the language delimits by indentation, by brace depth elsewhere. Research:
# `docs/research/2026-09-17-graph-one-live-walk-one-parse.md`.
_INDENT_BLOCK_LANGUAGES = frozenset({"python", "ruby"})


def _indent_of(text: str) -> int:
    return len(text) - len(text.lstrip())


def _header_end(lines: list[str], start: int) -> int:
    """The line where the declaration's own parentheses close (1-based)."""
    depth = 0
    for number in range(start, len(lines) + 1):
        depth += sum(_paren_delta(char) for char in lines[number - 1])
        if depth <= 0:
            return number
    return start


def _indent_block_end(lines: list[str], start: int) -> int:
    base = _indent_of(lines[start - 1])
    end = _header_end(lines, start)
    for number in range(end + 1, len(lines) + 1):
        text = lines[number - 1]
        if not text.strip():
            continue
        if _indent_of(text) <= base:
            break
        end = number
    return end


def _first_brace_line(lines: list[str], start: int) -> int | None:
    """The declaration line, or the next one when it opens the block alone."""
    if "{" in lines[start - 1]:
        return start
    if start < len(lines) and lines[start].lstrip().startswith("{"):
        return start + 1
    return None


def _brace_block_end(lines: list[str], start: int) -> int:
    opening = _first_brace_line(lines, start)
    if opening is None:
        return start
    depth = 0
    for number in range(opening, len(lines) + 1):
        text = lines[number - 1]
        depth += text.count("{") - text.count("}")
        if depth <= 0:
            return number
    return start


def _close_regex_blocks(lines: list[str], lang: str, symbols: list[dict]) -> None:
    block_end = _brace_block_end
    if lang in _INDENT_BLOCK_LANGUAGES:
        block_end = _indent_block_end
    for symbol in symbols:
        symbol["end_line"] = block_end(lines, symbol["line"])


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
    _close_regex_blocks(content.splitlines(), lang, [*functions, *classes])
    return {
        "file": str(file_path),
        "language": lang,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "imports": imports,
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


# The LSP document bound (`lsp_protocol.MAX_FRAME_BYTES`): nothing larger is a
# source a live answer should read.


def _parsable_names(parent: Path, names: list[str]) -> list[Path]:
    found = [parent / name for name in names]
    return [path for path in found if path.suffix.lower() in LANGUAGE_MAP and own_source_file(path)]


def live_source_files(directory: Path) -> list[Path]:
    """Every source the live fallback parses, under one directory rule.

    Audit 3, B27. Three walkers used to disagree here: one did not know `venv`,
    only one pruned hidden directories (so live `callers` still parsed every
    `.claude/` agent worktree), and all three tested the *absolute* path, so a
    repository checked out under `/x/venv/` had no definitions at all. The rule
    is `import_resolver.directory_skipped`, applied where `os.walk` meets a
    directory below the root: a skipped tree is never listed, and a name on the
    way to the root is never judged. Research:
    `docs/research/2026-09-17-graph-one-live-walk-one-parse.md`.
    """
    collected: list[Path] = []
    for current, directories, files in os.walk(directory):
        directories[:] = [name for name in directories if not directory_skipped(name)]
        collected.extend(_parsable_names(Path(current), files))
    return sorted(collected)


def parsed_live_sources(directory: Path) -> list[tuple[Path, dict]]:
    """The workspace parsed once; every live answer and its report share it."""
    registry = build_python_symbol_registry(directory)
    return [
        (path, parse_file_in_workspace(path, registry, directory))
        for path in live_source_files(directory)
    ]


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

    Returns stats: {files, functions, classes, calls, imports}. Read-only: the
    code index is `repository_index.py index <dir>`; the tool manifest this wrote
    was read by nothing and ran the repository's own `tsc` (audit C-44,
    docs/research/2026-09-25-navigation-dead-code-and-stale-words.md).
    """
    stats = {"files": 0, "functions": 0, "classes": 0, "calls": 0, "imports": 0}
    if not directory.exists():
        return stats
    for _path, result in parsed_live_sources(directory):
        _accumulate_index_stats(stats, result)
    if verbose:
        _print_index_stats(stats)
    return stats
