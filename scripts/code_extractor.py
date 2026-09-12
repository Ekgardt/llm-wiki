"""Pure, deterministic code extraction for Evidence Graph generations."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol


class _SourceRecord(Protocol):
    logical_id: str
    relative_path: str
    sha256: str
    size: int
    language: str | None


class _CapturedSource(Protocol):
    record: _SourceRecord
    content: bytes

EXTRACTOR_VERSION = "code-extractor/v12"
SCIP_DEFINITION_ROLE = 0x1
_SYNTAX_STOP_INTERVAL = 256
_MAX_OBSERVATION_TARGET_CHARS = 4096
_MAX_OBSERVATION_TARGET_BYTES = 4096
_GRAMMARS = {
    "bash": ("tree_sitter_bash", "language"),
    "c": ("tree_sitter_c", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "php": ("tree_sitter_php", "language_php"),
    "ruby": ("tree_sitter_ruby", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
}
_CLASS_TYPES = {
    "class_declaration", "class_definition", "enum_declaration", "interface_declaration",
    "struct_item", "struct_specifier", "trait_item", "type_declaration",
}
_FUNCTION_TYPES = {
    "function_declaration", "function_definition", "function_item", "method_declaration",
    "method_definition", "method", "singleton_method",
}
_CALL_TYPES = {"call", "call_expression", "command", "function_call", "invocation_expression"}
_IMPORT_TYPES = {
    "import_declaration", "import_from_statement", "import_header", "import_statement",
    "include_expression", "preproc_include", "require", "use_declaration",
}


def _check_stop(
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    _require_stop_arguments(deadline, cancelled)
    _raise_when_stopped(deadline, cancelled)


def _raise_when_stopped(
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if cancelled is not None and cancelled():
        raise TimeoutError("code extraction cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("code extraction deadline reached")


def _require_stop_arguments(deadline: object, cancelled: object) -> None:
    if deadline is not None and not _finite_number(deadline):
        raise ValueError("code extraction deadline must be finite or None")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("code extraction cancellation check must be callable or None")


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _byte_span(content: bytes, start: int, end: int) -> tuple[int, int, int, int]:
    return (
        start,
        end,
        content.count(b"\n", 0, start) + 1,
        content.count(b"\n", 0, end) + 1,
    )


def _whole_span(source: _CapturedSource) -> tuple[int, int, int, int]:
    return (0, len(source.content), 1, max(1, source.content.count(b"\n") + 1))


def _canonical_observation_target(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("observation target text must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("observation target text must not be empty")
    return _bounded_observation_text(normalized)


def _bounded_observation_text(normalized: str) -> str:
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("observation target text must be valid UTF-8") from exc
    if (
        len(normalized) <= _MAX_OBSERVATION_TARGET_CHARS
        and len(encoded) <= _MAX_OBSERVATION_TARGET_BYTES
    ):
        return normalized
    return _digest_truncated(normalized, encoded)


def _digest_truncated(normalized: str, encoded: bytes) -> str:
    digest = hashlib.sha256(encoded).hexdigest()
    suffix = f" ... [sha256:{digest}]"
    character_budget = _MAX_OBSERVATION_TARGET_CHARS - len(suffix)
    byte_budget = _MAX_OBSERVATION_TARGET_BYTES - len(suffix.encode("ascii"))
    prefix = normalized[:character_budget].encode("utf-8")[:byte_budget]
    return prefix.decode("utf-8", errors="ignore").rstrip() + suffix


def _optional_parser(language: str):
    """Build an isolated optional parser; absence is a normal degraded state."""
    specification = _GRAMMARS.get(language)
    if specification is None:
        return None
    try:
        import tree_sitter as ts

        module_name, factory_name = specification
        grammar = importlib.import_module(module_name)
        return ts.Parser(ts.Language(getattr(grammar, factory_name)()))
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ScipSymbol:
    """A compiler-backed symbol covering one source declaration."""

    source_id: str
    byte_start: int
    byte_end: int
    symbol: str
    roles: int = 0


@dataclass(frozen=True, slots=True)
class CoChange:
    """A precomputed, bounded repository co-change relationship."""

    source_path: str
    target_path: str
    weight: float
    evidence_source_id: str | None = None
    byte_start: int = 0
    byte_end: int = 0


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_sources: int = 10_000
    max_source_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_nodes: int = 250_000
    max_occurrences: int = 500_000
    max_assertions: int = 500_000
    max_evidence: int = 500_000
    max_observations: int = 250_000
    max_candidate_dependencies: int = 500_000
    max_scip_symbols: int = 500_000
    max_co_changes: int = 100_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if not _positive_limit(getattr(self, name)):
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class CodeExtraction:
    nodes: tuple[Mapping[str, object], ...]
    occurrences: tuple[Mapping[str, object], ...]
    assertions: tuple[Mapping[str, object], ...]
    evidence: tuple[Mapping[str, object], ...]
    observations: tuple[Mapping[str, object], ...]
    observation_source_dependencies: Mapping[str, tuple[str, ...]]


class _FrozenDict(dict):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("extraction records are immutable")

    __delitem__ = _immutable
    __setitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


_SCALARS = (str, int, float, type(None))


def _positive_limit(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _deep_freeze(value: object) -> object:
    if isinstance(value, _SCALARS):
        return value
    return _frozen_container(value)


def _frozen_container(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    return _frozen_collection(value)


def _frozen_collection(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _frozen(records: list[dict[str, object]], key: str) -> tuple[Mapping[str, object], ...]:
    records.sort(key=lambda item: str(item[key]))
    return tuple(_deep_freeze(record) for record in records)


def _frozen_observation_dependencies(
    dependencies: Mapping[str, set[str]],
) -> Mapping[str, tuple[str, ...]]:
    return _FrozenDict({key: tuple(sorted(value)) for key, value in sorted(dependencies.items())})


def _bounded_values(
    values: Iterable[object],
    maximum: int,
    label: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[object, ...]:
    retained = []
    for value in values:
        _check_stop(deadline, cancelled)
        if len(retained) >= maximum:
            raise ValueError(f"code extraction {label} ceiling exceeded")
        retained.append(value)
    return tuple(retained)


def _identifier(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"code:{prefix}:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _module_name(path: str) -> str:
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _line_offsets(content: bytes) -> tuple[int, ...]:
    offsets = [0]
    offsets.extend(index + 1 for index, byte in enumerate(content) if byte == 10)
    return tuple(offsets)


def _span(node: ast.AST, offsets: tuple[int, ...], content: bytes) -> tuple[int, int, int, int]:
    line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", line)
    column = getattr(node, "col_offset", 0)
    end_column = getattr(node, "end_col_offset", column)
    start = offsets[min(line - 1, len(offsets) - 1)] + column
    end = offsets[min(end_line - 1, len(offsets) - 1)] + end_column
    return start, min(end, len(content)), line, end_line


def _python_name_span(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    offsets: tuple[int, ...],
    content: bytes,
) -> tuple[int, int]:
    line_start = offsets[node.lineno - 1]
    declaration_end = content.find(b"\n", line_start)
    if declaration_end < 0:
        declaration_end = len(content)
    match = re.search(
        rb"\b(?:class|def|async\s+def)\s+" + re.escape(node.name.encode()) + rb"\b",
        content[line_start:declaration_end],
    )
    if match is None:
        return -1, -1
    name_offset = match.group(0).rfind(node.name.encode())
    start = line_start + match.start() + name_offset
    return start, start + len(node.name.encode())


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    arguments = [*node.args.posonlyargs, *node.args.args]
    if node.args.vararg:
        arguments.append(node.args.vararg)
    arguments.extend(node.args.kwonlyargs)
    if node.args.kwarg:
        arguments.append(node.args.kwarg)
    rendered = [f"{argument.arg}{_annotation_suffix(argument)}" for argument in arguments]
    return f"{node.name}({','.join(rendered)})"


def _annotation_suffix(argument: ast.arg) -> str:
    if argument.annotation is None:
        return ""
    return f":{ast.unparse(argument.annotation)}"


_ROUTE_MODULES = {
    "fastapi": {"APIRouter", "FastAPI"},
    "flask": {"Blueprint", "Flask"},
}
_ROUTE_METHODS = frozenset({"delete", "get", "patch", "post", "put", "route"})
_SQL_RELATIONSHIPS = (
    ("READS", r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)"),
    ("WRITES", r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_]\w*)"),
)
_TYPE_RELATIONSHIPS = (
    ("IMPLEMENTS", r"\bimplements\s+([A-Za-z_]\w*)"),
    ("INHERITS", r"\bextends\s+([A-Za-z_]\w*)"),
)
_QUALIFIED_CALL = r"\.|::|->"


@dataclass(frozen=True)
class _PythonFile:
    """One parsed Python source and where its module lives in the graph."""

    source: _CapturedSource
    offsets: tuple[int, ...]
    module_name: str
    module_id: str

    def span(self, node: ast.AST) -> tuple[int, int, int, int]:
        return _span(node, self.offsets, self.source.content)

    @property
    def is_package(self) -> bool:
        return PurePosixPath(self.source.record.relative_path).name == "__init__.py"


@dataclass(frozen=True)
class _SyntaxFile:
    """One tree-sitter source and where its module lives in the graph."""

    source: _CapturedSource
    language: str
    module_name: str
    module_id: str


def _defines_span(symbol: ScipSymbol, source: _CapturedSource, name_span: tuple[int, int]) -> bool:
    return (
        symbol.source_id == source.record.logical_id
        and bool(symbol.roles & SCIP_DEFINITION_ROLE)
        and (symbol.byte_start, symbol.byte_end) == tuple(name_span)
    )


def _add_module_aliases(module_aliases: dict[str, set[str]], module_name: str) -> None:
    module_parts = module_name.split(".")
    for offset in range(len(module_parts)):
        module_aliases.setdefault(".".join(module_parts[offset:]), set()).add(module_name)


def _sqlite_aliases(tree: ast.Module) -> set[str]:
    return {
        alias.asname or alias.name
        for alias in _top_level_imports(tree)
        if alias.name == "sqlite3"
    }


MAX_BINDINGS = 8
MAX_BINDING_BYTES = 256
MAX_ROUTE_PATH_BYTES = 512
_HTTP_CLIENT_MODULES = frozenset({"requests", "httpx"})
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _parameter_names(node: Mapping[str, object]) -> list[str]:
    """The callee's parameters, read from the signature the node already carries."""
    signature = str(node.get("metadata", {}).get("signature", ""))
    inside = signature.partition("(")[2].rpartition(")")[0]
    names = [part.partition(":")[0].strip() for part in inside.split(",")]
    return [name for name in names if name]


def _bound_parameters(node: Mapping[str, object]) -> list[str]:
    names = _parameter_names(node)
    if node.get("kind") == "method" and names:
        return names[1:]
    return names


def _passed_name(value: ast.expr) -> str | None:
    if isinstance(value, (ast.Name, ast.Attribute)):
        return ast.unparse(value)
    return None


def _positional_bindings(node: ast.Call, parameters: list[str]) -> list[str]:
    pairs = []
    for index, argument in enumerate(node.args):
        passed = _passed_name(argument)
        if passed is not None and index < len(parameters):
            pairs.append(f"{passed}->{parameters[index]}")
    return pairs


def _keyword_bindings(node: ast.Call, parameters: list[str]) -> list[str]:
    pairs = []
    for keyword in node.keywords:
        passed = _passed_name(keyword.value)
        if passed is not None and keyword.arg in parameters:
            pairs.append(f"{passed}->{keyword.arg}")
    return pairs


def _argument_bindings(node: ast.Call, target: Mapping[str, object]) -> str:
    """`argument->parameter` pairs of one call, bounded in count and bytes."""
    parameters = _bound_parameters(target)
    pairs = _positional_bindings(node, parameters) + _keyword_bindings(node, parameters)
    if not pairs:
        return ""
    return _bounded_bindings(pairs)


def _bounded_bindings(pairs: list[str]) -> str:
    kept = pairs[:MAX_BINDINGS]
    text = ",".join(kept)
    while len(text.encode("utf-8")) > MAX_BINDING_BYTES and kept:
        kept.pop()
        text = ",".join(kept)
    remaining = len(pairs) - len(kept)
    if not remaining:
        return text
    return f"{text}+{remaining} more".lstrip(",")


def _client_module(func: ast.expr, aliases: Mapping[str, tuple[str, str]]) -> str | None:
    """The HTTP client module a `module.method(...)` call names, if it is one."""
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    module, _symbol = aliases.get(func.value.id, ("", ""))
    if module.split(".")[0] not in _HTTP_CLIENT_MODULES:
        return None
    return module


def _request_path(node: ast.Call) -> str | None:
    target = _string_constant(next(iter(node.args), None))
    if target is None or len(target.encode("utf-8")) > MAX_ROUTE_PATH_BYTES:
        return None
    return _url_path(target)


def _url_path(target: str) -> str | None:
    if not target.startswith(("http://", "https://")):
        return target or None
    remainder = target.split("://", 1)[1]
    _host, separator, path = remainder.partition("/")
    if not separator:
        return None
    return f"/{path.split('?', 1)[0]}"


def _http_client_call(
    node: ast.Call, aliases: Mapping[str, tuple[str, str]]
) -> tuple[str, str] | None:
    """(METHOD, path) of an HTTP client call with a literal path, else None."""
    func = node.func
    if not _client_method(func, aliases):
        return None
    path = _request_path(node)
    if path is None:
        return None
    return func.attr.upper(), path


def _client_method(func: ast.expr, aliases: Mapping[str, tuple[str, str]]) -> bool:
    if not isinstance(func, ast.Attribute) or func.attr not in _HTTP_METHODS:
        return False
    return _client_module(func, aliases) is not None


def _top_level_imports(tree: ast.Module) -> list[ast.alias]:
    return [
        alias
        for statement in tree.body
        if isinstance(statement, ast.Import)
        for alias in statement.names
    ]


def _from_import_aliases(node: ast.ImportFrom, imported_module: str) -> dict[str, tuple[str, str]]:
    return {alias.asname or alias.name: (imported_module, alias.name) for alias in node.names}


def _assigned_targets(statement: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    return statement.targets if isinstance(statement, ast.Assign) else [statement.target]


def _assigned_names(statement: ast.Assign | ast.AnnAssign) -> list[str]:
    return [target.id for target in _assigned_targets(statement) if isinstance(target, ast.Name)]


def _imported_constructors(statement: ast.ImportFrom) -> set[str]:
    allowed = _ROUTE_MODULES[statement.module]
    return {alias.asname or alias.name for alias in statement.names if alias.name in allowed}


def _imported_route_modules(statement: ast.Import) -> dict[str, str]:
    return {
        alias.asname or alias.name: alias.name
        for alias in statement.names
        if alias.name in _ROUTE_MODULES
    }


def _collect_route_import(statement: ast.stmt, constructors: set[str], module_aliases: dict[str, str]) -> None:
    if isinstance(statement, ast.ImportFrom) and statement.module in _ROUTE_MODULES:
        constructors.update(_imported_constructors(statement))
        return
    if isinstance(statement, ast.Import):
        module_aliases.update(_imported_route_modules(statement))


def _route_constructors(tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    constructors: set[str] = set()
    module_aliases: dict[str, str] = {}
    for statement in tree.body:
        _collect_route_import(statement, constructors, module_aliases)
    return constructors, module_aliases


def _proven_route_constructor(constructor: ast.expr, constructors: set[str], module_aliases: dict[str, str]) -> bool:
    if isinstance(constructor, ast.Attribute) and isinstance(constructor.value, ast.Name):
        module = module_aliases.get(constructor.value.id)
        return module is not None and constructor.attr in _ROUTE_MODULES[module]
    return isinstance(constructor, ast.Name) and constructor.id in constructors


def _route_receiver_names(statement: ast.stmt, constructors: set[str], module_aliases: dict[str, str]) -> list[str]:
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or not isinstance(statement.value, ast.Call):
        return []
    if not _proven_route_constructor(statement.value.func, constructors, module_aliases):
        return []
    return _assigned_names(statement)


def _single_eq_main(test: ast.Compare) -> bool:
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq) or len(test.comparators) != 1:
        return False
    comparator = test.comparators[0]
    return isinstance(comparator, ast.Constant) and comparator.value == "__main__"


def _is_main_guard(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If):
        return False
    test = statement.test
    if not isinstance(test, ast.Compare) or not isinstance(test.left, ast.Name) or test.left.id != "__name__":
        return False
    return _single_eq_main(test)


def _called_names(statement: ast.stmt) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _string_constant(value: ast.expr | None) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _assigns_tablename(targets: list[ast.expr]) -> bool:
    return any(isinstance(target, ast.Name) and target.id == "__tablename__" for target in targets)


def _declared_table_name(statement: ast.stmt) -> str | None:
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
        return None
    if not _assigns_tablename(_assigned_targets(statement)):
        return None
    return _string_constant(statement.value)


def _route_decorator_function(decorator: ast.expr) -> ast.Attribute | None:
    if not isinstance(decorator, ast.Call) or not decorator.args:
        return None
    function = decorator.func
    if not isinstance(function, ast.Attribute) or function.attr.lower() not in _ROUTE_METHODS:
        return None
    return function


def _route_path(decorator: ast.Call, receiver: str | None, receivers: set[str]) -> str | None:
    if receiver not in receivers:
        return None
    return _string_constant(decorator.args[0])


def _call_fallback_reason(func: ast.expr, aliases: Mapping[str, tuple[str, str]]) -> str:
    if isinstance(func, ast.Name) and func.id in aliases:
        return "missing_dependency"
    if isinstance(func, ast.Attribute):
        return "dynamic_dispatch"
    return "unresolved_reference"


def _sql_literal(node: ast.Call) -> ast.Constant | None:
    if not node.args:
        return None
    statement = node.args[0]
    if _string_constant(statement) is None:
        return None
    return statement


def _literal_reference_span(content: bytes, table_name: str, literal_span: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    start = content.find(table_name.encode(), literal_span[0], literal_span[1])
    if start < 0:
        return None
    return _byte_span(content, start, start + len(table_name.encode()))


def _sqlite_connection_argument(argument: ast.arg, receiver: str, sqlite_aliases: set[str]) -> bool:
    annotation = argument.annotation
    if argument.arg != receiver or not isinstance(annotation, ast.Attribute):
        return False
    return (
        isinstance(annotation.value, ast.Name)
        and annotation.value.id in sqlite_aliases
        and annotation.attr == "Connection"
    )


def _syntax_name_node(node: object) -> object | None:
    named = node.child_by_field_name("name")
    if named is not None or node.type not in {"method", "singleton_method"}:
        return named
    return next((child for child in node.named_children if child.type in {"identifier", "constant"}), None)


def _syntax_name_span(node: object) -> tuple[int, int]:
    named = node.child_by_field_name("name")
    if named is None:
        return (-1, -1)
    return (named.start_byte, named.end_byte)


def _paren_step(character: str) -> int:
    return {"(": 1, ")": -1}.get(character, 0)


def _balanced_parentheses(declaration: str, start: int) -> str | None:
    """The text from `start` through its matching closing parenthesis, if there is one."""
    depth = 0
    for index in range(start, len(declaration)):
        depth += _paren_step(declaration[index])
        if declaration[index] == ")" and depth == 0:
            return declaration[start:index + 1]
    return None


def _innermost(records: Iterable[tuple], node: object) -> tuple | None:
    """The smallest recorded syntax node that encloses `node`."""
    containers = [
        item for item in records
        if item[0].start_byte <= node.start_byte and node.end_byte <= item[0].end_byte
    ]
    if not containers:
        return None
    return min(containers, key=lambda item: item[0].end_byte - item[0].start_byte)


def _syntax_owner(container: tuple | None, ctx: _SyntaxFile) -> tuple[str, str, str]:
    """(owner name, owner node, node kind) of a function inside `container` or at module level."""
    if container is None:
        return ctx.module_name, ctx.module_id, "function"
    return f"{ctx.module_name}.{container[2]}", container[1], "method"


def _supported_main(language: str, declaration: str) -> bool:
    return language in {"c", "cpp", "go", "rust"} or (
        language == "java" and "static" in declaration
    )


def _syntax_callee(node: object) -> object | None:
    function = node.child_by_field_name("function") or node.child_by_field_name("name")
    if function is None:
        function = next(iter(node.named_children), None)
    return function


def _single_local_target(candidates: tuple, shadowed: bool, qualified: bool) -> bool:
    return len(candidates) == 1 and not shadowed and not qualified


def _expression_alias(expression: ast.AST, aliases: Mapping[str, tuple[str, str]]) -> tuple[str, str] | None:
    if isinstance(expression, ast.Name):
        return aliases.get(expression.id)
    if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name):
        return aliases.get(expression.value.id)
    return None


def _alias_target(module: str, symbol: str, expression: ast.AST) -> str:
    if symbol and isinstance(expression, ast.Attribute):
        return f"{module}.{symbol}"
    return module


def _syntax_call_reason(candidates: tuple, shadowed: bool, qualified: bool) -> str:
    if len(candidates) > 1 and not shadowed:
        return "ambiguous_target"
    if qualified:
        return "dynamic_dispatch"
    return "unresolved_reference"


def _dispatch_syntax_node(
    ctx: _SyntaxFile,
    node: object,
    functions: Iterable[tuple[object, str]],
    handlers: Iterable[tuple[set[str], Callable[..., None]]],
) -> None:
    """Hand one syntax node to every edge pass whose node types include it, in order."""
    for types, handler in handlers:
        if node.type in types:
            handler(ctx, node, functions)


@dataclass(frozen=True)
class _PythonOwner:
    """The definition that encloses a Python statement while definitions are walked."""

    name: str
    node_id: str
    in_class: bool
    lexical_scope: str

    @property
    def definition_scope(self) -> str:
        return self.name if self.in_class else self.lexical_scope


class _Collector:
    def __init__(
        self,
        sources: tuple[_CapturedSource, ...],
        repository_id: str,
        scip_symbols: tuple[ScipSymbol, ...],
        limits: ExtractionLimits,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self.sources = sources
        self.repository_id = repository_id
        self.scip_symbols = scip_symbols
        self.limits = limits
        _require_stop_arguments(deadline, cancelled)
        self.deadline = deadline
        self.cancelled = cancelled
        self.nodes: dict[str, dict[str, object]] = {}
        self.occurrences: list[dict[str, object]] = []
        self.assertions: list[dict[str, object]] = []
        self.evidence: list[dict[str, object]] = []
        self.observations: list[dict[str, object]] = []
        self.observation_source_dependencies: dict[str, set[str]] = {}
        self.candidate_dependency_count = 0
        self.assertion_ids: set[str] = set()
        self.observation_ids: set[str] = set()
        self.modules: dict[str, list[str]] = {}
        self.module_name_index: dict[str, tuple[str, ...]] = {}
        self.source_modules: dict[str, str] = {}
        self.files: dict[str, str] = {}
        self.tables: dict[str, list[str]] = {}
        self.routes: dict[tuple[str, str], list[str]] = {}
        self.definitions: dict[tuple[str, str], list[str]] = {}
        self.python_scopes: dict[tuple[str, str, str], list[str]] = {}
        self.function_body_scope: dict[str, str] = {}
        self.function_parent_scope: dict[str, str] = {}
        self.scope_parent: dict[str, str] = {}
        self.route_receivers: dict[str, set[str]] = {}
        self.sqlite_modules: dict[str, set[str]] = {}
        self.python_entry_names: dict[str, set[str]] = {}
        self.node_ast: dict[int, str] = {}
        self.node_sources: dict[str, set[str]] = {}
        self.syntax_definitions: dict[tuple[str, str, str], list[tuple[object, str]]] = {}
        self.syntax_functions: dict[str, list[tuple[object, str]]] = {}

    def check(self, records: object, maximum: int, label: str) -> None:
        self.check_stop()
        if len(records) > maximum:  # type: ignore[arg-type]
            raise ValueError(f"code extraction {label} ceiling exceeded")

    def check_stop(self) -> None:
        """The stop checks alone: the constructor already validated both arguments."""
        _raise_when_stopped(self.deadline, self.cancelled)

    def add_node(
        self,
        kind: str,
        scheme: str,
        key: str,
        metadata: Mapping[str, object],
    ) -> str:
        node_id = _identifier("node", scheme, key)
        self.nodes.setdefault(node_id, {
            "node_id": node_id,
            "kind": kind,
            "identity_scheme": scheme,
            "identity_key": key,
            "metadata": dict(metadata),
        })
        self.check(self.nodes, self.limits.max_nodes, "node")
        return node_id

    def add_occurrence(
        self,
        node_id: str,
        source: _CapturedSource,
        role: str,
        span: tuple[int, int, int, int],
    ) -> None:
        start, end, line_start, line_end = span
        if end <= start:
            return
        occurrence_id = _identifier("occurrence", node_id, source.record.logical_id, role, start, end)
        self.occurrences.append({
            "occurrence_id": occurrence_id,
            "node_id": node_id,
            "source_id": source.record.logical_id,
            "role": role,
            "byte_start": start,
            "byte_end": end,
            "line_start": line_start,
            "line_end": line_end,
        })
        self.node_sources.setdefault(node_id, set()).add(source.record.logical_id)
        self.check(self.occurrences, self.limits.max_occurrences, "occurrence")

    def add_assertion(
        self,
        source_node_id: str,
        edge_type: str,
        target_node_id: str | None,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
        *,
        confidence: str = "high",
        literal: str | None = None,
    ) -> None:
        """One resolved assertion, naming either a target node or a literal.

        The graph contract allows exactly one of the two, so a literal
        assertion is joined to its node-to-node neighbour by the span both
        record as evidence.
        """
        start, end, _, _ = span
        if end <= start:
            return
        assertion_id = _identifier(
            "assertion", source_node_id, edge_type, target_node_id or literal or "",
            source.record.logical_id, start, end,
        )
        if assertion_id in self.assertion_ids:
            return
        self.assertion_ids.add(assertion_id)
        self.assertions.append({
            "assertion_id": assertion_id,
            "source_node_id": source_node_id,
            "edge_type": edge_type,
            "target_node_id": target_node_id,
            "literal": literal,
            "confidence": confidence,
            "authority": "ai-derived",
            "resolution": "resolved",
            "extractor": EXTRACTOR_VERSION,
        })
        self._add_evidence(source, start, end, assertion_id=assertion_id)
        self.check(self.assertions, self.limits.max_assertions, "assertion")

    def add_observation(
        self,
        source_node_id: str | None,
        edge_type: str,
        target_text: str | None,
        reason: str,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
        *,
        candidate_node_ids: Iterable[str] = (),
    ) -> None:
        start, end, _, _ = span
        if end <= start:
            return
        canonical_target = (
            None if target_text is None else _canonical_observation_target(target_text)
        )
        observation_id = _identifier(
            "observation", source_node_id, edge_type, canonical_target, reason,
            source.record.logical_id, start, end,
        )
        if observation_id in self.observation_ids:
            return
        self.observation_ids.add(observation_id)
        self.observations.append({
            "observation_id": observation_id,
            "source_node_id": source_node_id,
            "edge_type": edge_type,
            "target_text": canonical_target,
            "reason": reason,
            "extractor": EXTRACTOR_VERSION,
        })
        self._record_candidate_dependencies(observation_id, candidate_node_ids)
        self._add_evidence(source, start, end, observation_id=observation_id)
        self.check(self.observations, self.limits.max_observations, "observation")

    def _record_candidate_dependencies(
        self, observation_id: str, candidate_node_ids: Iterable[str]
    ) -> None:
        candidate_sources = {
            source_id
            for node_id in candidate_node_ids
            for source_id in self.node_sources.get(node_id, ())
        }
        if not candidate_sources:
            return
        self.candidate_dependency_count += len(candidate_sources)
        if self.candidate_dependency_count > self.limits.max_candidate_dependencies:
            raise ValueError("code extraction candidate dependency ceiling exceeded")
        self.observation_source_dependencies[observation_id] = candidate_sources

    def _add_evidence(
        self,
        source: _CapturedSource,
        start: int,
        end: int,
        *,
        assertion_id: str | None = None,
        observation_id: str | None = None,
    ) -> None:
        span = source.content[start:end]
        evidence_id = _identifier("evidence", assertion_id, observation_id)
        self.evidence.append({
            "evidence_id": evidence_id,
            "assertion_id": assertion_id,
            "observation_id": observation_id,
            "source_id": source.record.logical_id,
            "byte_start": start,
            "byte_end": end,
            "span_sha256": hashlib.sha256(span).hexdigest(),
        })
        self.check(self.evidence, self.limits.max_evidence, "evidence")

    def symbol_identity(
        self,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
        name_span: tuple[int, int],
        language: str,
        owner: str,
        name: str,
        signature: str,
    ) -> tuple[str, str]:
        candidates = sorted(
            symbol.symbol
            for symbol in self.scip_symbols
            if _defines_span(symbol, source, name_span)
        )
        if candidates:
            return "scip/v1", candidates[0]
        key = "\x1f".join((
            self.repository_id, language, source.record.relative_path,
            owner, name, signature,
        ))
        return "code-symbol/v1", key

    def structural_nodes(self) -> str:
        self.check_stop()
        repository = self.add_node(
            "repository", "repository/v1", self.repository_id,
            {"name": self.repository_id},
        )
        directories: dict[str, str] = {"": repository}
        module_aliases: dict[str, set[str]] = {}
        for source in self.sources:
            self.check_stop()
            self._structural_source(source, repository, directories, module_aliases)
        self.module_name_index = {
            alias: tuple(sorted(module_names))
            for alias, module_names in module_aliases.items()
        }
        return repository

    def _structural_source(
        self,
        source: _CapturedSource,
        repository: str,
        directories: dict[str, str],
        module_aliases: dict[str, set[str]],
    ) -> None:
        path = PurePosixPath(source.record.relative_path)
        whole = _whole_span(source)
        parent = self._directory_chain(source, path, repository, directories)
        file_node = self.add_node(
            "file", "repository-path/v1",
            f"{self.repository_id}\x1f{source.record.relative_path}",
            {"name": path.name, "path": source.record.relative_path},
        )
        self.files[source.record.relative_path] = file_node
        self.add_occurrence(file_node, source, "definition", whole)
        self.add_assertion(parent, "CONTAINS", file_node, source, whole)
        module_name = _module_name(source.record.relative_path)
        module = self.add_node(
            "module", "code-module/v1",
            f"{self.repository_id}\x1f{source.record.language or 'unknown'}\x1f"
            f"{module_name}\x1f{source.record.relative_path}",
            {"name": module_name, "path": source.record.relative_path},
        )
        self.modules.setdefault(module_name, []).append(module)
        _add_module_aliases(module_aliases, module_name)
        self.source_modules[source.record.logical_id] = module
        self.add_occurrence(module, source, "definition", whole)
        self.add_assertion(file_node, "DEFINES", module, source, whole)

    def _directory_chain(
        self,
        source: _CapturedSource,
        path: PurePosixPath,
        repository: str,
        directories: dict[str, str],
    ) -> str:
        whole = _whole_span(source)
        parent = repository
        accumulated: list[str] = []
        for part in path.parts[:-1]:
            accumulated.append(part)
            directory = self._directory(directories, part, "/".join(accumulated))
            self.add_assertion(parent, "CONTAINS", directory, source, whole)
            parent = directory
        return parent

    def _directory(self, directories: dict[str, str], part: str, directory_path: str) -> str:
        directory = directories.get(directory_path)
        if directory is not None:
            return directory
        directory = self.add_node(
            "directory", "repository-path/v1",
            f"{self.repository_id}\x1f{directory_path}",
            {"name": part, "path": directory_path},
        )
        directories[directory_path] = directory
        return directory

    def collect_python_definitions(self, source: _CapturedSource, tree: ast.Module) -> None:
        self.check_stop()
        module_name = _module_name(source.record.relative_path)
        module_id = self.source_modules[source.record.logical_id]
        self.route_receivers[source.record.logical_id] = self._python_route_receivers(tree)
        self.sqlite_modules[source.record.logical_id] = _sqlite_aliases(tree)
        self.python_entry_names[source.record.logical_id] = self._python_entry_names(tree)
        ctx = _PythonFile(source, _line_offsets(source.content), module_name, module_id)
        self._walk_python(
            ctx, tree.body, _PythonOwner(module_name or "<module>", module_id, False, module_name)
        )

    def _walk_python(self, ctx: _PythonFile, body: list[ast.stmt], owner: _PythonOwner) -> None:
        for node in body:
            self.check_stop()
            self._python_definition(ctx, node, owner)

    def _python_definition(self, ctx: _PythonFile, node: ast.stmt, owner: _PythonOwner) -> None:
        if isinstance(node, ast.ClassDef):
            self._python_class(ctx, node, owner)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._python_function(ctx, node, owner)

    def _python_class(self, ctx: _PythonFile, node: ast.ClassDef, owner: _PythonOwner) -> None:
        source = ctx.source
        span = ctx.span(node)
        name_span = _python_name_span(node, ctx.offsets, source.content)
        scheme, key = self.symbol_identity(
            source, span, name_span, "python", owner.name, node.name, node.name,
        )
        node_id = self.add_node(
            "class", scheme, key,
            {"name": node.name, "owner": owner.name, "path": source.record.relative_path},
        )
        self._register_python_definition(ctx, node, node_id, owner)
        self.add_occurrence(node_id, source, "definition", span)
        self.add_assertion(owner.node_id, "DEFINES", node_id, source, span)
        self._table(node, node_id, owner.name, source, ctx.offsets)
        self._walk_python(
            ctx, node.body,
            _PythonOwner(f"{owner.name}.{node.name}", node_id, True, owner.lexical_scope),
        )

    def _python_function(
        self,
        ctx: _PythonFile,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        owner: _PythonOwner,
    ) -> None:
        source = ctx.source
        span = ctx.span(node)
        name_span = _python_name_span(node, ctx.offsets, source.content)
        signature = _signature(node)
        scheme, key = self.symbol_identity(
            source, span, name_span, "python", owner.name, node.name, signature,
        )
        kind = "method" if owner.in_class else "function"
        node_id = self.add_node(
            kind, scheme, key,
            {
                "name": node.name, "owner": owner.name,
                "signature": signature, "path": source.record.relative_path,
            },
        )
        self._register_python_definition(ctx, node, node_id, owner)
        body_scope = f"{owner.name}.{node.name}"
        self.function_body_scope[node_id] = body_scope
        self.function_parent_scope[node_id] = owner.lexical_scope
        self.scope_parent[body_scope] = owner.lexical_scope
        self.add_occurrence(node_id, source, "definition", span)
        self.add_assertion(owner.node_id, "DEFINES", node_id, source, span)
        self._entry_point(node, node_id, owner.name, source, span)
        self._routes(node, node_id, owner.name, source, ctx.offsets)
        self._walk_python(ctx, node.body, _PythonOwner(body_scope, node_id, False, body_scope))

    def _register_python_definition(
        self, ctx: _PythonFile, node: ast.AST, node_id: str, owner: _PythonOwner
    ) -> None:
        self.node_ast[id(node)] = node_id
        self.python_scopes.setdefault(
            (ctx.module_name, owner.definition_scope, node.name), []
        ).append(node_id)
        if not owner.in_class and owner.lexical_scope == ctx.module_name:
            self.definitions.setdefault((ctx.module_name, node.name), []).append(node_id)

    @staticmethod
    def _python_route_receivers(tree: ast.Module) -> set[str]:
        constructors, module_aliases = _route_constructors(tree)
        receivers: set[str] = set()
        for statement in tree.body:
            receivers.update(_route_receiver_names(statement, constructors, module_aliases))
        return receivers

    @staticmethod
    def _python_entry_names(tree: ast.Module) -> set[str]:
        names: set[str] = set()
        for statement in tree.body:
            if _is_main_guard(statement):
                names.update(_called_names(statement))
        return names

    def _table(
        self,
        node: ast.ClassDef,
        class_id: str,
        owner: str,
        source: _CapturedSource,
        offsets: tuple[int, ...],
    ) -> None:
        for statement in node.body:
            self.check_stop()
            table_name = _declared_table_name(statement)
            if table_name is not None:
                self._add_table(statement, table_name, class_id, owner, source, offsets)

    def _add_table(
        self,
        statement: ast.stmt,
        table_name: str,
        class_id: str,
        owner: str,
        source: _CapturedSource,
        offsets: tuple[int, ...],
    ) -> None:
        span = _span(statement, offsets, source.content)
        key = f"{self.repository_id}\x1f{table_name}"
        table = self.add_node("table", "database-table/v1", key, {"name": table_name, "owner": owner})
        self.tables.setdefault(table_name.casefold(), []).append(table)
        self.add_occurrence(table, source, "definition", span)
        self.add_assertion(class_id, "DEFINES", table, source, span)

    def _entry_point(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        function_id: str,
        owner: str,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
    ) -> None:
        if node.name != "main":
            return
        if node.name not in self.python_entry_names[source.record.logical_id]:
            self.add_observation(
                function_id, "EXPOSES", node.name, "unsupported_semantics", source, span
            )
            return
        key = f"{self.repository_id}\x1f{source.record.relative_path}\x1f{owner}\x1fmain"
        entry = self.add_node("entry-point", "code-entry-point/v1", key, {"name": "main", "kind": "main"})
        self.add_occurrence(entry, source, "definition", span)
        self.add_assertion(function_id, "EXPOSES", entry, source, span)

    def _routes(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        function_id: str,
        owner: str,
        source: _CapturedSource,
        offsets: tuple[int, ...],
    ) -> None:
        for decorator in node.decorator_list:
            function = _route_decorator_function(decorator)
            if function is not None:
                self._route(decorator, function, f"{owner}.{node.name}", function_id, source, offsets)

    def _route(
        self,
        decorator: ast.Call,
        function: ast.Attribute,
        qualified_name: str,
        function_id: str,
        source: _CapturedSource,
        offsets: tuple[int, ...],
    ) -> None:
        receiver = function.value.id if isinstance(function.value, ast.Name) else None
        span = _span(decorator, offsets, source.content)
        path = _route_path(decorator, receiver, self.route_receivers[source.record.logical_id])
        if path is None:
            self.add_observation(
                function_id, "EXPOSES", ast.unparse(decorator),
                "unsupported_semantics", source, span,
            )
            return
        method = function.attr.upper()
        key = f"{self.repository_id}\x1f{method}\x1f{path}\x1f{qualified_name}"
        route = self.add_node(
            "route", "code-route/v1", key,
            {"name": f"{method} {path}", "method": method, "path": path},
        )
        self.routes.setdefault((method, path), []).append(route)
        self.add_occurrence(route, source, "definition", span)
        self.add_assertion(function_id, "EXPOSES", route, source, span)

    def collect_python_edges(self, source: _CapturedSource, tree: ast.Module) -> None:
        self.check_stop()
        ctx = _PythonFile(
            source,
            _line_offsets(source.content),
            _module_name(source.record.relative_path),
            self.source_modules[source.record.logical_id],
        )
        aliases = self._import_aliases(ctx, tree)
        parent = self._parent_map(tree)
        for node in ast.walk(tree):
            self.check_stop()
            if isinstance(node, (ast.ClassDef, ast.Call)):
                self._python_node_edges(ctx, node, aliases, parent)

    def _import_aliases(self, ctx: _PythonFile, tree: ast.Module) -> dict[str, tuple[str, str]]:
        aliases: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            self.check_stop()
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._record_import(ctx, node, aliases)
        return aliases

    def _record_import(
        self, ctx: _PythonFile, node: ast.AST, aliases: dict[str, tuple[str, str]]
    ) -> None:
        if isinstance(node, ast.Import):
            self._plain_import(ctx, node, aliases)
            return
        if isinstance(node, ast.ImportFrom):
            self._from_import(ctx, node, aliases)

    def _plain_import(
        self, ctx: _PythonFile, node: ast.Import, aliases: dict[str, tuple[str, str]]
    ) -> None:
        for alias in node.names:
            aliases[alias.asname or alias.name.split(".")[0]] = (alias.name, "")
            self._import_edge(ctx.module_id, alias.name, ctx.source, ctx.span(node))

    def _from_import(
        self, ctx: _PythonFile, node: ast.ImportFrom, aliases: dict[str, tuple[str, str]]
    ) -> None:
        imported_module = self._absolute_import(
            ctx.module_name,
            node.module or "",
            node.level,
            is_package=ctx.is_package,
        )
        if node.module is None and node.level > 0:
            self._relative_names_import(ctx, node, imported_module, aliases)
            return
        self._import_edge(ctx.module_id, imported_module, ctx.source, ctx.span(node))
        aliases.update(_from_import_aliases(node, imported_module))

    def _relative_names_import(
        self,
        ctx: _PythonFile,
        node: ast.ImportFrom,
        imported_module: str,
        aliases: dict[str, tuple[str, str]],
    ) -> None:
        for alias in node.names:
            target, symbol = self._relative_name_target(imported_module, alias.name)
            self._import_edge(ctx.module_id, target, ctx.source, ctx.span(node))
            aliases[alias.asname or alias.name] = (target, symbol)

    def _relative_name_target(self, imported_module: str, name: str) -> tuple[str, str]:
        """`from . import name` names a submodule unless the package itself defines `name`."""
        submodule = f"{imported_module}.{name}"
        package_symbol = self.definitions.get((imported_module, name), ())
        if name != "*" and (self._matching_modules(submodule) or not package_symbol):
            return submodule, ""
        return imported_module, name

    def _parent_map(self, tree: ast.Module) -> dict[int, ast.AST]:
        parent: dict[int, ast.AST] = {}
        for candidate in ast.walk(tree):
            self.check_stop()
            for child in ast.iter_child_nodes(candidate):
                parent[id(child)] = candidate
        return parent

    def _python_node_edges(
        self,
        ctx: _PythonFile,
        node: ast.AST,
        aliases: Mapping[str, tuple[str, str]],
        parent: Mapping[int, ast.AST],
    ) -> None:
        if isinstance(node, ast.ClassDef):
            self._inheritance_edges(ctx, node, aliases)
        if isinstance(node, ast.Call) and not self._is_route_decorator(node, parent):
            self._call_edges(ctx, node, aliases, parent)

    def _inheritance_edges(
        self, ctx: _PythonFile, node: ast.ClassDef, aliases: Mapping[str, tuple[str, str]]
    ) -> None:
        class_id = self.node_ast.get(id(node))
        if not class_id:
            return
        for base in node.bases:
            targets = self._resolve_expression(base, ctx.module_name, aliases)
            self._resolved_edge(
                class_id, "INHERITS", targets, ast.unparse(base), ctx.source, ctx.span(base),
                "unresolved_reference",
            )

    def _call_edges(
        self,
        ctx: _PythonFile,
        node: ast.Call,
        aliases: Mapping[str, tuple[str, str]],
        parent: Mapping[int, ast.AST],
    ) -> None:
        owner = self._enclosing_node(node, parent)
        source_node_id = self.node_ast.get(id(owner), ctx.module_id) if owner else ctx.module_id
        span = ctx.span(node)
        targets = self._resolve_expression(node.func, ctx.module_name, aliases, owner)
        self._resolved_edge(
            source_node_id, "CALLS", targets, ast.unparse(node.func), ctx.source, span,
            _call_fallback_reason(node.func, aliases),
            fallback_candidates=self._candidate_modules(node.func, aliases),
        )
        self._binding_edge(node, targets, source_node_id, ctx.source, span)
        self._http_edge(ctx, node, aliases, source_node_id, span)
        self._sql_edges(node, owner, source_node_id, ctx.source, ctx.offsets)

    def _binding_edge(
        self,
        node: ast.Call,
        targets: list[str],
        source_node_id: str,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
    ) -> None:
        """What the caller passes, and to which parameter of the one callee.

        A binding with no proven callee is not evidence, so an unresolved or
        ambiguous call records nothing here (research
        2026-09-11-argument-bindings-and-route-calls.md).
        """
        if len(targets) != 1:
            return
        bindings = _argument_bindings(node, self.nodes[targets[0]])
        if not bindings:
            return
        self.add_assertion(
            source_node_id, "BINDS_ARGUMENTS", None, source, span, literal=bindings
        )

    def _http_edge(
        self,
        ctx: _PythonFile,
        node: ast.Call,
        aliases: Mapping[str, tuple[str, str]],
        source_node_id: str,
        span: tuple[int, int, int, int],
    ) -> None:
        """A client call with a literal path names the route it reaches."""
        call = _http_client_call(node, aliases)
        if call is None:
            return
        method, path = call
        routes = self.routes.get((method, path), ())
        self._resolved_edge(
            source_node_id, "HTTP_CALLS", routes, f"{method} {path}", ctx.source, span,
            "unresolved_reference", confidence="medium",
        )

    def _resolved_edge(
        self,
        source_node_id: str,
        edge_type: str,
        targets: list[str] | tuple[str, ...],
        text: str,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
        fallback_reason: str,
        *,
        fallback_candidates: Iterable[str] = (),
        confidence: str = "high",
    ) -> None:
        """One target is an assertion, several an ambiguity, none the fallback observation."""
        if len(targets) == 1:
            self.add_assertion(
                source_node_id, edge_type, targets[0], source, span, confidence=confidence
            )
            return
        if len(targets) > 1:
            self.add_observation(
                source_node_id, edge_type, text, "ambiguous_target", source, span,
                candidate_node_ids=targets,
            )
            return
        self.add_observation(
            source_node_id, edge_type, text, fallback_reason, source, span,
            candidate_node_ids=fallback_candidates,
        )

    def _sql_edges(
        self,
        node: ast.Call,
        owner: ast.AST | None,
        source_node_id: str,
        source: _CapturedSource,
        offsets: tuple[int, ...],
    ) -> None:
        statement = _sql_literal(node)
        if statement is None:
            return
        supported_api = self._supported_sql_api(node.func, owner, source)
        literal_span = _span(statement, offsets, source.content)
        for edge_type, pattern in _SQL_RELATIONSHIPS:
            for match in re.finditer(pattern, statement.value, re.IGNORECASE):
                self.check_stop()
                self._table_edge(
                    source_node_id, edge_type, match.group(1), supported_api, source, literal_span
                )

    def _supported_sql_api(
        self, function: ast.expr, owner: ast.AST | None, source: _CapturedSource
    ) -> bool:
        if not isinstance(function, ast.Attribute) or function.attr not in {"execute", "executemany"}:
            return False
        if not isinstance(function.value, ast.Name):
            return False
        return self._sqlite_receiver(source, owner, function.value.id)

    def _table_edge(
        self,
        source_node_id: str,
        edge_type: str,
        table_name: str,
        supported_api: bool,
        source: _CapturedSource,
        literal_span: tuple[int, int, int, int],
    ) -> None:
        reference_span = _literal_reference_span(source.content, table_name, literal_span)
        if reference_span is None:
            return
        if not supported_api:
            self.add_observation(
                source_node_id, edge_type, table_name,
                "unsupported_semantics", source, reference_span,
            )
            return
        tables = self.tables.get(table_name.casefold(), ())
        self._resolved_edge(
            source_node_id, edge_type, tables, table_name, source, reference_span,
            "unresolved_reference",
        )

    def _sqlite_receiver(
        self, source: _CapturedSource, owner: ast.AST | None, receiver: str
    ) -> bool:
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        arguments = [
            *owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs,
        ]
        sqlite_aliases = self.sqlite_modules[source.record.logical_id]
        return any(
            _sqlite_connection_argument(argument, receiver, sqlite_aliases)
            for argument in arguments
        )

    @staticmethod
    def _syntax_span(node: object) -> tuple[int, int, int, int]:
        return (
            node.start_byte,
            node.end_byte,
            node.start_point[0] + 1,
            node.end_point[0] + 1,
        )

    def _syntax_nodes(self, root: object, maximum: int) -> list[object]:
        nodes: list[object] = []
        pending = [root]
        while pending:
            if len(nodes) % _SYNTAX_STOP_INTERVAL == 0:
                self.check_stop()
            node = pending.pop()
            nodes.append(node)
            if len(nodes) > maximum:
                raise ValueError("code extraction syntax node ceiling exceeded")
            pending.extend(reversed(node.named_children))
        self.check_stop()
        return nodes

    @staticmethod
    def _syntax_name(node: object, content: bytes) -> str | None:
        named = _syntax_name_node(node)
        if named is None:
            return None
        return content[named.start_byte:named.end_byte].decode("utf-8", errors="strict")

    @staticmethod
    def _syntax_signature(node: object, name: str, content: bytes) -> str:
        declaration = content[node.start_byte:node.end_byte].decode("utf-8", errors="strict")
        match = re.search(rf"\b{re.escape(name)}\s*\(", declaration)
        if match is None:
            return name
        parameters = _balanced_parentheses(declaration, declaration.find("(", match.start()))
        if parameters is None:
            return name
        return name + re.sub(r"\s+", " ", parameters)

    def collect_syntax_definitions(self, source: _CapturedSource, root: object) -> None:
        self.check_stop()
        ctx = self._syntax_file(source)
        nodes = self._syntax_nodes(root, self.limits.max_occurrences * 4)
        classes = self._syntax_classes(ctx, nodes)
        for node in nodes:
            self.check_stop()
            self._syntax_function(ctx, node, classes)

    def _syntax_file(self, source: _CapturedSource) -> _SyntaxFile:
        return _SyntaxFile(
            source,
            source.record.language or "unknown",
            _module_name(source.record.relative_path),
            self.source_modules[source.record.logical_id],
        )

    def _syntax_classes(self, ctx: _SyntaxFile, nodes: list[object]) -> list[tuple[object, str, str]]:
        classes: list[tuple[object, str, str]] = []
        for node in nodes:
            self.check_stop()
            record = self._syntax_class(ctx, node)
            if record is not None:
                classes.append(record)
        return classes

    def _syntax_class(self, ctx: _SyntaxFile, node: object) -> tuple[object, str, str] | None:
        if node.type not in _CLASS_TYPES:
            return None
        name = self._syntax_name(node, ctx.source.content)
        if not name:
            return None
        return self._add_syntax_class(ctx, node, name)

    def _add_syntax_class(self, ctx: _SyntaxFile, node: object, name: str) -> tuple[object, str, str]:
        source = ctx.source
        span = self._syntax_span(node)
        scheme, key = self.symbol_identity(
            source, span, _syntax_name_span(node), ctx.language, ctx.module_name, name, name
        )
        node_id = self.add_node(
            "class", scheme, key,
            {"name": name, "owner": ctx.module_name, "path": source.record.relative_path},
        )
        self.syntax_definitions.setdefault(
            (ctx.language, ctx.module_name, name), []
        ).append((node, node_id))
        self.add_occurrence(node_id, source, "definition", span)
        self.add_assertion(ctx.module_id, "DEFINES", node_id, source, span)
        return node, node_id, name

    def _syntax_function(
        self, ctx: _SyntaxFile, node: object, classes: list[tuple[object, str, str]]
    ) -> None:
        if node.type not in _FUNCTION_TYPES:
            return
        name = self._syntax_name(node, ctx.source.content)
        if not name:
            return
        self._add_syntax_function(ctx, node, name, _innermost(classes, node))

    def _add_syntax_function(
        self, ctx: _SyntaxFile, node: object, name: str, container: tuple | None
    ) -> None:
        source = ctx.source
        owner_name, owner_id, kind = _syntax_owner(container, ctx)
        span = self._syntax_span(node)
        signature = self._syntax_signature(node, name, source.content)
        scheme, key = self.symbol_identity(
            source, span, _syntax_name_span(node), ctx.language, owner_name, name, signature
        )
        node_id = self.add_node(
            kind, scheme, key,
            {
                "name": name, "owner": owner_name, "signature": signature,
                "path": source.record.relative_path,
            },
        )
        self.syntax_definitions.setdefault(
            (ctx.language, ctx.module_name, name), []
        ).append((node, node_id))
        self.syntax_functions.setdefault(source.record.logical_id, []).append((node, node_id))
        self.add_occurrence(node_id, source, "definition", span)
        self.add_assertion(owner_id, "DEFINES", node_id, source, span)
        if name == "main":
            self._syntax_main(ctx, node, node_id, owner_name, span)

    def _syntax_main(
        self,
        ctx: _SyntaxFile,
        node: object,
        node_id: str,
        owner_name: str,
        span: tuple[int, int, int, int],
    ) -> None:
        source = ctx.source
        declaration = source.content[node.start_byte:node.end_byte].decode("utf-8", errors="strict")
        if not _supported_main(ctx.language, declaration):
            self.add_observation(node_id, "EXPOSES", "main", "unsupported_semantics", source, span)
            return
        key = f"{self.repository_id}\x1f{source.record.relative_path}\x1f{owner_name}\x1fmain"
        entry = self.add_node(
            "entry-point", "code-entry-point/v1", key, {"name": "main", "kind": "main"},
        )
        self.add_occurrence(entry, source, "definition", span)
        self.add_assertion(node_id, "EXPOSES", entry, source, span)

    def collect_syntax_edges(self, source: _CapturedSource, root: object) -> None:
        self.check_stop()
        ctx = self._syntax_file(source)
        functions = self.syntax_functions.get(source.record.logical_id, ())
        handlers = (
            (_CLASS_TYPES, self._syntax_class_edges),
            (_IMPORT_TYPES, self._syntax_import_edge),
            (_CALL_TYPES, self._syntax_call_edge),
        )
        for node in self._syntax_nodes(root, self.limits.max_occurrences * 4):
            self.check_stop()
            _dispatch_syntax_node(ctx, node, functions, handlers)

    def _syntax_class_edges(self, ctx: _SyntaxFile, node: object, _functions: object) -> None:
        self._syntax_type_edges(ctx, node)

    def _syntax_import_edge(self, ctx: _SyntaxFile, node: object, _functions: object) -> None:
        text = ctx.source.content[node.start_byte:node.end_byte].decode("utf-8", errors="strict")
        target_text = self._import_target(text)
        self._resolved_edge(
            ctx.module_id, "IMPORTS", self._module_candidates(target_text), target_text or text,
            ctx.source, self._syntax_span(node), "missing_dependency", confidence="medium",
        )

    def _syntax_call_edge(
        self, ctx: _SyntaxFile, node: object, functions: Iterable[tuple[object, str]]
    ) -> None:
        function = _syntax_callee(node)
        if function is None:
            return
        text = ctx.source.content[function.start_byte:function.end_byte].decode(
            "utf-8", errors="strict"
        )
        self._syntax_call(ctx, node, text, functions)

    def _syntax_call(
        self, ctx: _SyntaxFile, node: object, text: str, functions: Iterable[tuple[object, str]]
    ) -> None:
        name = re.split(_QUALIFIED_CALL, text)[-1]
        candidates = self._syntax_function_candidates(ctx, name)
        owner_record = _innermost(functions, node)
        source_node = ctx.module_id if owner_record is None else owner_record[1]
        shadowed = owner_record is not None and self._syntax_shadowed(
            owner_record[0], node, name, ctx.source.content
        )
        span = self._syntax_span(node)
        qualified = re.search(_QUALIFIED_CALL, text) is not None
        if _single_local_target(candidates, shadowed, qualified):
            self.add_assertion(
                source_node, "CALLS", candidates[0][1], ctx.source, span, confidence="medium"
            )
            return
        reason = _syntax_call_reason(candidates, shadowed, qualified)
        self.add_observation(source_node, "CALLS", text, reason, ctx.source, span)

    def _syntax_function_candidates(self, ctx: _SyntaxFile, name: str) -> tuple:
        return tuple(
            item
            for item in self.syntax_definitions.get((ctx.language, ctx.module_name, name), ())
            if self.nodes[item[1]]["kind"] == "function"
        )

    @staticmethod
    def _syntax_shadowed(owner: object, call: object, name: str, content: bytes) -> bool:
        parameters = owner.child_by_field_name("parameters")
        if parameters is not None and re.search(
            rf"\b{re.escape(name)}\b",
            content[parameters.start_byte:parameters.end_byte].decode("utf-8", errors="strict"),
        ):
            return True
        prefix = content[owner.start_byte:call.start_byte].decode("utf-8", errors="strict")
        return bool(re.search(rf"\b(?:const|let|var)\s+{re.escape(name)}\b", prefix))

    def _syntax_type_edges(self, ctx: _SyntaxFile, node: object) -> None:
        source_node = self._syntax_definition_at(ctx, node)
        if source_node is None:
            return
        declaration = ctx.source.content[node.start_byte:node.end_byte].decode(
            "utf-8", errors="strict"
        )
        for edge_type, pattern in _TYPE_RELATIONSHIPS:
            for match in re.finditer(pattern, declaration):
                self._syntax_type_edge(ctx, source_node, edge_type, node, match)

    def _syntax_definition_at(self, ctx: _SyntaxFile, node: object) -> str | None:
        name = self._syntax_name(node, ctx.source.content)
        owners = self.syntax_definitions.get((ctx.language, ctx.module_name, name or ""), ())
        return next(
            (node_id for candidate, node_id in owners if candidate.start_byte == node.start_byte),
            None,
        )

    def _syntax_type_edge(
        self,
        ctx: _SyntaxFile,
        source_node: str,
        edge_type: str,
        node: object,
        match: re.Match[str],
    ) -> None:
        targets = self.syntax_definitions.get((ctx.language, ctx.module_name, match.group(1)), ())
        span = _byte_span(
            ctx.source.content, node.start_byte + match.start(1), node.start_byte + match.end(1)
        )
        if len(targets) == 1:
            self.add_assertion(
                source_node, edge_type, targets[0][1], ctx.source, span, confidence="medium",
            )
            return
        reason = "ambiguous_target" if len(targets) > 1 else "unresolved_reference"
        self.add_observation(source_node, edge_type, match.group(1), reason, ctx.source, span)

    @staticmethod
    def _import_target(text: str) -> str:
        quoted = re.search(r"['\"]([^'\"]+)['\"]", text)
        if quoted:
            return quoted.group(1).removeprefix("./").replace("/", ".").removesuffix(".js")
        match = re.search(r"\b(?:import|use|using)\s+([\w.:]+)", text)
        return "" if match is None else match.group(1).replace("::", ".").rstrip(";")

    @staticmethod
    def _absolute_import(
        module_name: str,
        imported: str,
        level: int,
        *,
        is_package: bool = False,
    ) -> str:
        if level == 0:
            return imported
        package = module_name.split(".") if is_package else module_name.split(".")[:-1]
        if level > len(package):
            return f"{'.' * level}{imported}"
        keep = len(package) - level + 1
        return ".".join([*package[:keep], *([imported] if imported else [])])

    def _import_edge(
        self,
        module_id: str,
        imported: str,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
    ) -> None:
        targets = self._module_candidates(imported)
        self._resolved_edge(
            module_id, "IMPORTS", targets, imported, source, span, "missing_dependency"
        )

    def _matching_modules(self, imported: str) -> tuple[str, ...]:
        return self.module_name_index.get(imported, ())

    def _module_candidates(self, imported: str) -> list[str]:
        return [
            node_id
            for module in self._matching_modules(imported)
            for node_id in self.modules[module]
        ]

    def _candidate_modules(
        self,
        expression: ast.AST,
        aliases: Mapping[str, tuple[str, str]],
    ) -> tuple[str, ...]:
        alias = _expression_alias(expression, aliases)
        if alias is None:
            return ()
        module, symbol = alias
        return tuple(self._module_candidates(_alias_target(module, symbol, expression)))

    def _resolve_expression(
        self,
        expression: ast.AST,
        module_name: str,
        aliases: Mapping[str, tuple[str, str]],
        owner: ast.AST | None = None,
    ) -> list[str]:
        if isinstance(expression, ast.Name):
            return self._resolve_name(expression, module_name, aliases, owner)
        if isinstance(expression, ast.Attribute):
            return self._resolve_attribute(expression, module_name, aliases, owner)
        return []

    def _resolve_name(
        self,
        expression: ast.Name,
        module_name: str,
        aliases: Mapping[str, tuple[str, str]],
        owner: ast.AST | None,
    ) -> list[str]:
        if expression.id in aliases:
            return self._alias_targets(*aliases[expression.id])
        if owner is None:
            return list(self.python_scopes.get((module_name, module_name, expression.id), ()))
        return self._scoped_targets(expression.id, module_name, owner)

    def _alias_targets(self, module: str, symbol: str) -> list[str]:
        if symbol:
            return self._symbol_targets(module, symbol)
        return self._module_candidates(module)

    def _symbol_targets(self, module: str, symbol: str) -> list[str]:
        return [
            node_id
            for candidate in self._matching_modules(module)
            for node_id in self.definitions.get((candidate, symbol), ())
        ]

    def _scoped_targets(self, name: str, module_name: str, owner: ast.AST) -> list[str]:
        """The innermost enclosing scope that defines `name`, falling back to module level."""
        owner_id = self.node_ast.get(id(owner))
        scope = None if owner_id is None else self.function_body_scope.get(owner_id)
        visited = set()
        while scope is not None and scope not in visited:
            visited.add(scope)
            candidates = self.python_scopes.get((module_name, scope, name), ())
            if candidates:
                return list(candidates)
            scope = self.scope_parent.get(scope)
        return list(self.python_scopes.get((module_name, module_name, name), ()))

    def _resolve_attribute(
        self,
        expression: ast.Attribute,
        module_name: str,
        aliases: Mapping[str, tuple[str, str]],
        owner: ast.AST | None,
    ) -> list[str]:
        value = expression.value
        if not isinstance(value, ast.Name):
            return []
        if value.id == "self" and owner:
            return self._self_method_targets(expression.attr, module_name, owner)
        return self._named_attribute_targets(value.id, expression.attr, module_name, aliases)

    def _named_attribute_targets(
        self, name: str, attr: str, module_name: str, aliases: Mapping[str, tuple[str, str]]
    ) -> list[str]:
        if name in aliases:
            module, symbol = aliases[name]
            return self._symbol_targets(f"{module}.{symbol}" if symbol else module, attr)
        return self._class_attribute_targets(name, attr, module_name)

    def _self_method_targets(self, attr: str, module_name: str, owner: ast.AST) -> list[str]:
        owner_id = self.node_ast.get(id(owner))
        owner_name = "" if owner_id is None else str(
            self.nodes[owner_id]["metadata"].get("owner", "")
        )
        return [
            node_id
            for node_id in self.python_scopes.get((module_name, owner_name, attr), ())
            if self._owned_method(node_id, owner_name)
        ]

    def _owned_method(self, node_id: str, owner_name: str) -> bool:
        node = self.nodes[node_id]
        return node["kind"] == "method" and node["metadata"].get("owner") == owner_name

    def _class_attribute_targets(self, name: str, attr: str, module_name: str) -> list[str]:
        if not self.definitions.get((module_name, name), ()):
            return []
        return [
            node_id
            for node_id in self.python_scopes.get((module_name, f"{module_name}.{name}", attr), ())
            if self.nodes[node_id]["metadata"].get("owner", "").endswith(name)
        ]

    @staticmethod
    def _enclosing_node(node: ast.AST, parent: Mapping[int, ast.AST]) -> ast.AST | None:
        current = parent.get(id(node))
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = parent.get(id(current))
        return None

    @staticmethod
    def _is_route_decorator(node: ast.Call, parent: Mapping[int, ast.AST]) -> bool:
        current = parent.get(id(node))
        return isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)) and node in current.decorator_list

    def extract(self) -> CodeExtraction:
        self.check_stop()
        self.structural_nodes()
        parsed: list[tuple[_CapturedSource, ast.Module]] = []
        syntax_trees: list[tuple[_CapturedSource, object]] = []
        for source in self.sources:
            self.check_stop()
            self._parse_source(source, parsed, syntax_trees)
        for source, tree in parsed:
            self.check_stop()
            self.collect_python_edges(source, tree)
        for source, root in syntax_trees:
            self.check_stop()
            self.collect_syntax_edges(source, root)
        self.check_stop()
        return self.result()

    def result(self) -> CodeExtraction:
        return CodeExtraction(
            _frozen(list(self.nodes.values()), "node_id"),
            _frozen(self.occurrences, "occurrence_id"),
            _frozen(self.assertions, "assertion_id"),
            _frozen(self.evidence, "evidence_id"),
            _frozen(self.observations, "observation_id"),
            _frozen_observation_dependencies(self.observation_source_dependencies),
        )

    def _parse_source(
        self,
        source: _CapturedSource,
        parsed: list[tuple[_CapturedSource, ast.Module]],
        syntax_trees: list[tuple[_CapturedSource, object]],
    ) -> None:
        if source.record.language != "python":
            self._parse_syntax_source(source, syntax_trees)
            return
        tree = self._parsed_python(source)
        if tree is None:
            return
        parsed.append((source, tree))
        self.collect_python_definitions(source, tree)

    def _parsed_python(self, source: _CapturedSource) -> ast.Module | None:
        try:
            self.check_stop()
            tree = ast.parse(source.content, filename=source.record.relative_path)
            self.check_stop()
        except (SyntaxError, ValueError, UnicodeError) as exc:
            self.add_observation(
                self.source_modules[source.record.logical_id],
                "PARSES", str(exc), "parse_error", source, _whole_span(source),
            )
            return None
        return tree

    def _parse_syntax_source(
        self, source: _CapturedSource, syntax_trees: list[tuple[_CapturedSource, object]]
    ) -> None:
        language = source.record.language
        module_id = self.source_modules[source.record.logical_id]
        parser = _optional_parser(language or "")
        if parser is None:
            self.add_observation(
                module_id, "PARSES", language, "unsupported_semantics", source, _whole_span(source),
            )
            return
        self.check_stop()
        tree = parser.parse(source.content)
        self.check_stop()
        if tree.root_node.has_error:
            self.add_observation(
                module_id, "PARSES", language, "parse_error", source, _whole_span(source),
            )
            return
        syntax_trees.append((source, tree.root_node))
        self.collect_syntax_definitions(source, tree.root_node)


def extract_code(
    sources: Iterable[_CapturedSource],
    *,
    repository_id: str,
    scip_symbols: Iterable[ScipSymbol] = (),
    co_changes: Iterable[CoChange] = (),
    limits: ExtractionLimits | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CodeExtraction:
    """Extract immutable source snapshots without filesystem or store access."""
    _require_repository_id(repository_id)
    bounds = limits or ExtractionLimits()
    _check_stop(deadline, cancelled)
    captured = _bounded_values(sources, bounds.max_sources, "source", deadline, cancelled)
    _require_captured_shape(captured)
    selected = tuple(sorted(captured, key=lambda item: item.record.relative_path))
    _require_unique_sources(selected)
    _require_source_bytes(selected, bounds, deadline, cancelled)
    symbols = _bounded_values(
        scip_symbols,
        bounds.max_scip_symbols,
        "SCIP symbol",
        deadline,
        cancelled,
    )
    _require_valid_symbols(symbols, selected, deadline, cancelled)
    collector = _Collector(
        selected,
        repository_id,
        tuple(sorted(symbols, key=lambda item: item.symbol)),
        bounds,
        deadline,
        cancelled,
    )
    result = collector.extract()
    changes = _bounded_values(
        co_changes,
        bounds.max_co_changes,
        "co-change",
        deadline,
        cancelled,
    )
    _require_valid_co_changes(changes)
    if not changes:
        return result
    _add_co_changes(collector, changes, selected, deadline, cancelled)
    return collector.result()


_REQUIRED_RECORD_FIELDS = ("logical_id", "relative_path", "sha256", "size", "language")


def _require_repository_id(repository_id: object) -> None:
    if not isinstance(repository_id, str) or not repository_id or len(repository_id) > 512:
        raise ValueError("repository_id must be a bounded non-empty string")


def _captured_shape(source: object) -> bool:
    if not hasattr(source, "record") or not hasattr(source, "content"):
        return False
    return all(hasattr(source.record, field) for field in _REQUIRED_RECORD_FIELDS)


def _require_captured_shape(captured: tuple[object, ...]) -> None:
    if not all(_captured_shape(source) for source in captured):
        raise TypeError("sources must contain immutable captured source values")


def _require_unique_sources(selected: tuple[_CapturedSource, ...]) -> None:
    if len({source.record.relative_path for source in selected}) != len(selected):
        raise ValueError("code extraction source paths must be unique")
    if len({source.record.logical_id for source in selected}) != len(selected):
        raise ValueError("code extraction source IDs must be unique")


def _require_source_bytes(
    selected: tuple[_CapturedSource, ...],
    bounds: ExtractionLimits,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    total = 0
    for source in selected:
        _check_stop(deadline, cancelled)
        total += _checked_source_size(source, bounds, total)


def _checked_source_size(source: _CapturedSource, bounds: ExtractionLimits, total: int) -> int:
    _require_source_shape(source)
    size = len(source.content)
    if size > bounds.max_source_bytes or total + size > bounds.max_total_bytes:
        raise ValueError("code extraction source byte ceiling exceeded")
    _require_recorded_content(source)
    return size


def _require_source_shape(source: _CapturedSource) -> None:
    if not isinstance(source.content, bytes):
        raise TypeError("captured source content must be bytes")
    if not _canonical_relative(source.record.relative_path):
        raise ValueError("captured source path must be canonical and repository-relative")


def _canonical_relative(relative: object) -> bool:
    if not _bounded_relative_text(relative):
        return False
    pure = PurePosixPath(relative)
    return not pure.is_absolute() and not any(part in {"", ".", ".."} for part in pure.parts)


def _bounded_relative_text(relative: object) -> bool:
    return (
        isinstance(relative, str)
        and bool(relative)
        and len(relative) <= 4096
        and "\\" not in relative
    )


def _require_recorded_content(source: _CapturedSource) -> None:
    if source.record.size != len(source.content):
        raise ValueError("captured source size does not match content")
    if source.record.sha256 != hashlib.sha256(source.content).hexdigest():
        raise ValueError("captured source hash does not match content")


def _require_valid_symbols(
    symbols: tuple[object, ...],
    selected: tuple[_CapturedSource, ...],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    sources_by_id = {source.record.logical_id: source for source in selected}
    for symbol in symbols:
        _check_stop(deadline, cancelled)
        if not _valid_symbol(symbol, sources_by_id):
            raise ValueError("SCIP symbols must identify a valid captured source span")


def _valid_symbol(symbol: object, sources_by_id: Mapping[str, _CapturedSource]) -> bool:
    source = sources_by_id.get(getattr(symbol, "source_id", None))
    if not isinstance(symbol, ScipSymbol) or source is None:
        return False
    return _valid_symbol_span(symbol, len(source.content)) and _valid_symbol_name(symbol)


def _valid_symbol_span(symbol: ScipSymbol, content_length: int) -> bool:
    if not _non_negative_int(symbol.byte_start) or not isinstance(symbol.byte_end, int):
        return False
    return symbol.byte_start < symbol.byte_end <= content_length


def _valid_symbol_name(symbol: ScipSymbol) -> bool:
    if not symbol.symbol or len(symbol.symbol) > 4096:
        return False
    return _non_negative_int(symbol.roles) and symbol.roles <= 0x7F


def _require_valid_co_changes(changes: tuple[object, ...]) -> None:
    if not all(_valid_co_change(change) for change in changes):
        raise ValueError("co-change records must use bounded finite values")


def _valid_co_change(change: object) -> bool:
    if not isinstance(change, CoChange) or not math.isfinite(change.weight):
        return False
    if not 0.0 <= change.weight <= 1.0:
        return False
    return _bounded_co_change_paths(change)


def _bounded_co_change_paths(change: CoChange) -> bool:
    if not change.source_path or not change.target_path:
        return False
    return len(change.source_path) <= 4096 and len(change.target_path) <= 4096


def _add_co_changes(
    collector: _Collector,
    changes: tuple[CoChange, ...],
    selected: tuple[_CapturedSource, ...],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    for change in sorted(changes, key=lambda item: (item.source_path, item.target_path)):
        _check_stop(deadline, cancelled)
        _add_co_change(collector, change, selected)


def _add_co_change(
    collector: _Collector, change: CoChange, selected: tuple[_CapturedSource, ...]
) -> None:
    evidence_source = _evidence_source(selected, change.evidence_source_id)
    source_node = collector.files.get(change.source_path)
    target_node = collector.files.get(change.target_path)
    if not _all_present(evidence_source, source_node, target_node):
        return
    if not _co_change_anchored(change, evidence_source):
        return
    collector.add_assertion(
        source_node, "CO_CHANGED_WITH", target_node, evidence_source,
        _byte_span(evidence_source.content, change.byte_start, change.byte_end),
        confidence="medium",
    )


def _evidence_source(
    selected: tuple[_CapturedSource, ...], logical_id: str | None
) -> _CapturedSource | None:
    return next((source for source in selected if source.record.logical_id == logical_id), None)


def _all_present(*values: object) -> bool:
    return all(value is not None for value in values)


def _co_change_anchored(change: CoChange, evidence_source: _CapturedSource) -> bool:
    return (
        0 <= change.byte_start < change.byte_end <= len(evidence_source.content)
        and 0.0 <= change.weight <= 1.0
    )


extract_sources = extract_code
