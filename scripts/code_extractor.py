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

EXTRACTOR_VERSION = "code-extractor/v11"
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
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("code extraction deadline must be finite or None")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("code extraction cancellation check must be callable or None")
    if cancelled is not None and cancelled():
        raise TimeoutError("code extraction cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("code extraction deadline reached")


def _canonical_observation_target(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("observation target text must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("observation target text must not be empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("observation target text must be valid UTF-8") from exc
    if (
        len(normalized) <= _MAX_OBSERVATION_TARGET_CHARS
        and len(encoded) <= _MAX_OBSERVATION_TARGET_BYTES
    ):
        return normalized

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
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
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


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
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
    rendered = []
    for argument in arguments:
        annotation = ""
        if argument.annotation is not None:
            annotation = f":{ast.unparse(argument.annotation)}"
        rendered.append(f"{argument.arg}{annotation}")
    return f"{node.name}({','.join(rendered)})"


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
        _check_stop(self.deadline, self.cancelled)

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
        target_node_id: str,
        source: _CapturedSource,
        span: tuple[int, int, int, int],
        *,
        confidence: str = "high",
    ) -> None:
        start, end, _, _ = span
        if end <= start:
            return
        assertion_id = _identifier(
            "assertion", source_node_id, edge_type, target_node_id,
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
            "literal": None,
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
        candidate_sources = {
            source_id
            for node_id in candidate_node_ids
            for source_id in self.node_sources.get(node_id, ())
        }
        if candidate_sources:
            self.candidate_dependency_count += len(candidate_sources)
            if self.candidate_dependency_count > self.limits.max_candidate_dependencies:
                raise ValueError("code extraction candidate dependency ceiling exceeded")
            self.observation_source_dependencies[observation_id] = candidate_sources
        self._add_evidence(source, start, end, observation_id=observation_id)
        self.check(self.observations, self.limits.max_observations, "observation")

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
        name_start, name_end = name_span
        candidates = sorted(
            symbol.symbol
            for symbol in self.scip_symbols
            if symbol.source_id == source.record.logical_id
            and symbol.roles & SCIP_DEFINITION_ROLE
            and (symbol.byte_start, symbol.byte_end) == (name_start, name_end)
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
            path = PurePosixPath(source.record.relative_path)
            parent = repository
            accumulated: list[str] = []
            whole = (0, len(source.content), 1, max(1, source.content.count(b"\n") + 1))
            for part in path.parts[:-1]:
                accumulated.append(part)
                directory_path = "/".join(accumulated)
                directory = directories.get(directory_path)
                if directory is None:
                    directory = self.add_node(
                        "directory", "repository-path/v1",
                        f"{self.repository_id}\x1f{directory_path}",
                        {"name": part, "path": directory_path},
                    )
                    directories[directory_path] = directory
                self.add_assertion(parent, "CONTAINS", directory, source, whole)
                parent = directory
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
            module_parts = module_name.split(".")
            for offset in range(len(module_parts)):
                alias = ".".join(module_parts[offset:])
                module_aliases.setdefault(alias, set()).add(module_name)
            self.source_modules[source.record.logical_id] = module
            self.add_occurrence(module, source, "definition", whole)
            self.add_assertion(file_node, "DEFINES", module, source, whole)
        self.module_name_index = {
            alias: tuple(sorted(module_names))
            for alias, module_names in module_aliases.items()
        }
        return repository

    def collect_python_definitions(self, source: _CapturedSource, tree: ast.Module) -> None:
        self.check_stop()
        offsets = _line_offsets(source.content)
        module_name = _module_name(source.record.relative_path)
        module_id = self.source_modules[source.record.logical_id]
        self.route_receivers[source.record.logical_id] = self._python_route_receivers(tree)
        self.sqlite_modules[source.record.logical_id] = {
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.Import)
            for alias in statement.names
            if alias.name == "sqlite3"
        }
        self.python_entry_names[source.record.logical_id] = self._python_entry_names(tree)

        def walk(
            body: list[ast.stmt],
            owner_name: str,
            owner_id: str,
            in_class: bool,
            lexical_scope: str,
        ) -> None:
            for node in body:
                self.check_stop()
                if isinstance(node, ast.ClassDef):
                    span = _span(node, offsets, source.content)
                    name_span = _python_name_span(node, offsets, source.content)
                    scheme, key = self.symbol_identity(
                        source, span, name_span, "python", owner_name, node.name, node.name,
                    )
                    node_id = self.add_node(
                        "class", scheme, key,
                        {"name": node.name, "owner": owner_name, "path": source.record.relative_path},
                    )
                    self.node_ast[id(node)] = node_id
                    definition_scope = owner_name if in_class else lexical_scope
                    self.python_scopes.setdefault(
                        (module_name, definition_scope, node.name), []
                    ).append(node_id)
                    if not in_class and lexical_scope == module_name:
                        self.definitions.setdefault((module_name, node.name), []).append(node_id)
                    self.add_occurrence(node_id, source, "definition", span)
                    self.add_assertion(owner_id, "DEFINES", node_id, source, span)
                    self._table(node, node_id, owner_name, source, offsets)
                    walk(
                        node.body, f"{owner_name}.{node.name}", node_id, True,
                        lexical_scope,
                    )
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    span = _span(node, offsets, source.content)
                    name_span = _python_name_span(node, offsets, source.content)
                    signature = _signature(node)
                    scheme, key = self.symbol_identity(
                        source, span, name_span, "python", owner_name, node.name, signature,
                    )
                    kind = "method" if in_class else "function"
                    node_id = self.add_node(
                        kind, scheme, key,
                        {
                            "name": node.name, "owner": owner_name,
                            "signature": signature, "path": source.record.relative_path,
                        },
                    )
                    self.node_ast[id(node)] = node_id
                    definition_scope = owner_name if in_class else lexical_scope
                    self.python_scopes.setdefault(
                        (module_name, definition_scope, node.name), []
                    ).append(node_id)
                    if not in_class and lexical_scope == module_name:
                        self.definitions.setdefault((module_name, node.name), []).append(node_id)
                    body_scope = f"{owner_name}.{node.name}"
                    self.function_body_scope[node_id] = body_scope
                    self.function_parent_scope[node_id] = lexical_scope
                    self.scope_parent[body_scope] = lexical_scope
                    self.add_occurrence(node_id, source, "definition", span)
                    self.add_assertion(owner_id, "DEFINES", node_id, source, span)
                    self._entry_point(node, node_id, owner_name, source, span)
                    self._routes(node, node_id, owner_name, source, offsets)
                    walk(node.body, body_scope, node_id, False, body_scope)

        walk(tree.body, module_name or "<module>", module_id, False, module_name)

    @staticmethod
    def _python_route_receivers(tree: ast.Module) -> set[str]:
        supported_modules = {
            "fastapi": {"APIRouter", "FastAPI"},
            "flask": {"Blueprint", "Flask"},
        }
        constructors = set()
        module_aliases = {}
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module in supported_modules:
                constructors.update(
                    alias.asname or alias.name
                    for alias in statement.names
                    if alias.name in supported_modules[statement.module]
                )
            elif isinstance(statement, ast.Import):
                module_aliases.update({
                    alias.asname or alias.name: alias.name
                    for alias in statement.names
                    if alias.name in supported_modules
                })
        receivers = set()
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            value = statement.value
            if not isinstance(value, ast.Call):
                continue
            constructor = value.func
            proven = isinstance(constructor, ast.Name) and constructor.id in constructors
            if isinstance(constructor, ast.Attribute) and isinstance(constructor.value, ast.Name):
                module = module_aliases.get(constructor.value.id)
                proven = module is not None and constructor.attr in supported_modules[module]
            if not proven:
                continue
            receivers.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
        return receivers

    @staticmethod
    def _python_entry_names(tree: ast.Module) -> set[str]:
        names = set()
        for statement in tree.body:
            if not isinstance(statement, ast.If):
                continue
            test = statement.test
            if not (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            ):
                continue
            for node in ast.walk(statement):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    names.add(node.func.id)
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
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            value = statement.value
            if not any(isinstance(target, ast.Name) and target.id == "__tablename__" for target in targets):
                continue
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            span = _span(statement, offsets, source.content)
            key = f"{self.repository_id}\x1f{value.value}"
            table = self.add_node("table", "database-table/v1", key, {"name": value.value, "owner": owner})
            self.tables.setdefault(value.value.casefold(), []).append(table)
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
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            function = decorator.func
            if not isinstance(function, ast.Attribute) or function.attr.lower() not in {
                "delete", "get", "patch", "post", "put", "route",
            }:
                continue
            receiver = function.value.id if isinstance(function.value, ast.Name) else None
            span = _span(decorator, offsets, source.content)
            if receiver not in self.route_receivers[source.record.logical_id]:
                self.add_observation(
                    function_id, "EXPOSES", ast.unparse(decorator),
                    "unsupported_semantics", source, span,
                )
                continue
            path = decorator.args[0]
            if not isinstance(path, ast.Constant) or not isinstance(path.value, str):
                self.add_observation(
                    function_id, "EXPOSES", ast.unparse(decorator),
                    "unsupported_semantics", source, span,
                )
                continue
            method = function.attr.upper()
            key = f"{self.repository_id}\x1f{method}\x1f{path.value}\x1f{owner}.{node.name}"
            route = self.add_node(
                "route", "code-route/v1", key,
                {"name": f"{method} {path.value}", "method": method, "path": path.value},
            )
            self.add_occurrence(route, source, "definition", span)
            self.add_assertion(function_id, "EXPOSES", route, source, span)

    def collect_python_edges(self, source: _CapturedSource, tree: ast.Module) -> None:
        self.check_stop()
        offsets = _line_offsets(source.content)
        module_name = _module_name(source.record.relative_path)
        module_id = self.source_modules[source.record.logical_id]
        aliases: dict[str, tuple[str, str]] = {}

        for node in ast.walk(tree):
            self.check_stop()
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = (alias.name, "")
                    self._import_edge(module_id, alias.name, source, _span(node, offsets, source.content))
            elif isinstance(node, ast.ImportFrom):
                imported_module = self._absolute_import(
                    module_name,
                    node.module or "",
                    node.level,
                    is_package=PurePosixPath(source.record.relative_path).name
                    == "__init__.py",
                )
                if node.module is None and node.level > 0:
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        submodule = f"{imported_module}.{alias.name}"
                        package_symbol = self.definitions.get(
                            (imported_module, alias.name), ()
                        )
                        if alias.name != "*" and (
                            self._matching_modules(submodule) or not package_symbol
                        ):
                            self._import_edge(
                                module_id,
                                submodule,
                                source,
                                _span(node, offsets, source.content),
                            )
                            aliases[local_name] = (submodule, "")
                        else:
                            self._import_edge(
                                module_id,
                                imported_module,
                                source,
                                _span(node, offsets, source.content),
                            )
                            aliases[local_name] = (imported_module, alias.name)
                    continue
                self._import_edge(module_id, imported_module, source, _span(node, offsets, source.content))
                for alias in node.names:
                    aliases[alias.asname or alias.name] = (imported_module, alias.name)

        parent: dict[int, ast.AST] = {}
        for candidate in ast.walk(tree):
            self.check_stop()
            for child in ast.iter_child_nodes(candidate):
                parent[id(child)] = candidate

        for node in ast.walk(tree):
            self.check_stop()
            if isinstance(node, ast.ClassDef):
                class_id = self.node_ast.get(id(node))
                if class_id:
                    for base in node.bases:
                        target = self._resolve_expression(base, module_name, aliases)
                        span = _span(base, offsets, source.content)
                        if len(target) == 1:
                            self.add_assertion(class_id, "INHERITS", target[0], source, span)
                        elif len(target) > 1:
                            self.add_observation(
                                class_id,
                                "INHERITS",
                                ast.unparse(base),
                                "ambiguous_target",
                                source,
                                span,
                                candidate_node_ids=target,
                            )
                        else:
                            self.add_observation(
                                class_id, "INHERITS", ast.unparse(base),
                                "unresolved_reference", source, span,
                            )
            if not isinstance(node, ast.Call) or self._is_route_decorator(node, parent):
                continue
            owner = self._enclosing_node(node, parent)
            source_node_id = self.node_ast.get(id(owner), module_id) if owner else module_id
            span = _span(node, offsets, source.content)
            targets = self._resolve_expression(node.func, module_name, aliases, owner)
            text = ast.unparse(node.func)
            if len(targets) == 1:
                self.add_assertion(source_node_id, "CALLS", targets[0], source, span)
            elif len(targets) > 1:
                self.add_observation(
                    source_node_id,
                    "CALLS",
                    text,
                    "ambiguous_target",
                    source,
                    span,
                    candidate_node_ids=targets,
                )
            else:
                reason = "dynamic_dispatch" if isinstance(node.func, ast.Attribute) else "unresolved_reference"
                if isinstance(node.func, ast.Name) and node.func.id in aliases:
                    reason = "missing_dependency"
                self.add_observation(
                    source_node_id,
                    "CALLS",
                    text,
                    reason,
                    source,
                    span,
                    candidate_node_ids=self._candidate_modules(node.func, aliases),
                )
            self._sql_edges(node, owner, source_node_id, source, offsets)

    def _sql_edges(
        self,
        node: ast.Call,
        owner: ast.AST | None,
        source_node_id: str,
        source: _CapturedSource,
        offsets: tuple[int, ...],
    ) -> None:
        if not node.args:
            return
        statement = node.args[0]
        if not isinstance(statement, ast.Constant) or not isinstance(statement.value, str):
            return
        sql = statement.value
        function = node.func
        receiver = (
            function.value.id
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
            else None
        )
        supported_api = (
            isinstance(function, ast.Attribute)
            and function.attr in {"execute", "executemany"}
            and receiver is not None
            and self._sqlite_receiver(source, owner, receiver)
        )
        relationships = (
            ("READS", r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)"),
            ("WRITES", r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_]\w*)"),
        )
        for edge_type, pattern in relationships:
            for match in re.finditer(pattern, sql, re.IGNORECASE):
                self.check_stop()
                table_name = match.group(1)
                literal_span = _span(statement, offsets, source.content)
                start = source.content.find(
                    table_name.encode(), literal_span[0], literal_span[1]
                )
                if start < 0:
                    continue
                end = start + len(table_name.encode())
                reference_span = (
                    start,
                    end,
                    source.content.count(b"\n", 0, start) + 1,
                    source.content.count(b"\n", 0, end) + 1,
                )
                tables = self.tables.get(table_name.casefold(), ())
                if not supported_api:
                    self.add_observation(
                        source_node_id, edge_type, table_name,
                        "unsupported_semantics", source, reference_span,
                    )
                elif not tables:
                    self.add_observation(
                        source_node_id, edge_type, table_name,
                        "unresolved_reference", source, reference_span,
                    )
                elif len(tables) == 1:
                    self.add_assertion(
                        source_node_id, edge_type, tables[0], source, reference_span
                    )
                else:
                    self.add_observation(
                        source_node_id,
                        edge_type,
                        table_name,
                        "ambiguous_target",
                        source,
                        reference_span,
                        candidate_node_ids=tables,
                    )

    def _sqlite_receiver(
        self, source: _CapturedSource, owner: ast.AST | None, receiver: str
    ) -> bool:
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        arguments = [
            *owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs,
        ]
        return any(
            argument.arg == receiver
            and isinstance(argument.annotation, ast.Attribute)
            and isinstance(argument.annotation.value, ast.Name)
            and argument.annotation.value.id in self.sqlite_modules[source.record.logical_id]
            and argument.annotation.attr == "Connection"
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
        named = node.child_by_field_name("name")
        if named is None and node.type in {"method", "singleton_method"}:
            named = next((child for child in node.named_children if child.type in {"identifier", "constant"}), None)
        if named is None:
            return None
        return content[named.start_byte:named.end_byte].decode("utf-8", errors="strict")

    @staticmethod
    def _syntax_signature(node: object, name: str, content: bytes) -> str:
        declaration = content[node.start_byte:node.end_byte].decode("utf-8", errors="strict")
        match = re.search(rf"\b{re.escape(name)}\s*\(", declaration)
        if match is None:
            return name
        start = declaration.find("(", match.start())
        depth = 0
        for index in range(start, len(declaration)):
            if declaration[index] == "(":
                depth += 1
            elif declaration[index] == ")":
                depth -= 1
                if depth == 0:
                    parameters = re.sub(r"\s+", " ", declaration[start:index + 1])
                    return f"{name}{parameters}"
        return name

    def collect_syntax_definitions(self, source: _CapturedSource, root: object) -> None:
        self.check_stop()
        language = source.record.language or "unknown"
        module_name = _module_name(source.record.relative_path)
        module_id = self.source_modules[source.record.logical_id]
        nodes = self._syntax_nodes(root, self.limits.max_occurrences * 4)
        classes: list[tuple[object, str, str]] = []
        for node in nodes:
            self.check_stop()
            if node.type not in _CLASS_TYPES:
                continue
            name = self._syntax_name(node, source.content)
            if not name:
                continue
            span = self._syntax_span(node)
            named = node.child_by_field_name("name")
            name_span = (
                (named.start_byte, named.end_byte) if named is not None else (-1, -1)
            )
            scheme, key = self.symbol_identity(
                source, span, name_span, language, module_name, name, name
            )
            node_id = self.add_node(
                "class", scheme, key,
                {"name": name, "owner": module_name, "path": source.record.relative_path},
            )
            classes.append((node, node_id, name))
            self.syntax_definitions.setdefault(
                (language, module_name, name), []
            ).append((node, node_id))
            self.add_occurrence(node_id, source, "definition", span)
            self.add_assertion(module_id, "DEFINES", node_id, source, span)
        for node in nodes:
            self.check_stop()
            if node.type not in _FUNCTION_TYPES:
                continue
            name = self._syntax_name(node, source.content)
            if not name:
                continue
            containers = [item for item in classes if item[0].start_byte <= node.start_byte and node.end_byte <= item[0].end_byte]
            container = min(containers, key=lambda item: item[0].end_byte - item[0].start_byte) if containers else None
            owner_name = f"{module_name}.{container[2]}" if container else module_name
            owner_id = container[1] if container else module_id
            span = self._syntax_span(node)
            signature = self._syntax_signature(node, name, source.content)
            named = node.child_by_field_name("name")
            name_span = (
                (named.start_byte, named.end_byte) if named is not None else (-1, -1)
            )
            scheme, key = self.symbol_identity(
                source, span, name_span, language, owner_name, name, signature
            )
            node_id = self.add_node(
                "method" if container else "function", scheme, key,
                {
                    "name": name, "owner": owner_name, "signature": signature,
                    "path": source.record.relative_path,
                },
            )
            self.syntax_definitions.setdefault(
                (language, module_name, name), []
            ).append((node, node_id))
            self.syntax_functions.setdefault(source.record.logical_id, []).append((node, node_id))
            self.add_occurrence(node_id, source, "definition", span)
            self.add_assertion(owner_id, "DEFINES", node_id, source, span)
            if name == "main":
                declaration = source.content[node.start_byte:node.end_byte].decode(
                    "utf-8", errors="strict"
                )
                supported = language in {"c", "cpp", "go", "rust"} or (
                    language == "java" and "static" in declaration
                )
                if supported:
                    key = f"{self.repository_id}\x1f{source.record.relative_path}\x1f{owner_name}\x1fmain"
                    entry = self.add_node(
                        "entry-point", "code-entry-point/v1", key,
                        {"name": "main", "kind": "main"},
                    )
                    self.add_occurrence(entry, source, "definition", span)
                    self.add_assertion(node_id, "EXPOSES", entry, source, span)
                else:
                    self.add_observation(
                        node_id, "EXPOSES", name, "unsupported_semantics", source, span
                    )

    def collect_syntax_edges(self, source: _CapturedSource, root: object) -> None:
        self.check_stop()
        language = source.record.language or "unknown"
        module_name = _module_name(source.record.relative_path)
        module_id = self.source_modules[source.record.logical_id]
        functions = self.syntax_functions.get(source.record.logical_id, ())
        for node in self._syntax_nodes(root, self.limits.max_occurrences * 4):
            self.check_stop()
            if node.type in _CLASS_TYPES:
                self._syntax_type_edges(node, source)
            if node.type in _IMPORT_TYPES:
                text = source.content[node.start_byte:node.end_byte].decode("utf-8", errors="strict")
                target_text = self._import_target(text)
                targets = self._module_candidates(target_text)
                span = self._syntax_span(node)
                if len(targets) == 1:
                    self.add_assertion(
                        module_id, "IMPORTS", targets[0], source, span, confidence="medium"
                    )
                elif len(targets) > 1:
                    self.add_observation(
                        module_id,
                        "IMPORTS",
                        target_text or text,
                        "ambiguous_target",
                        source,
                        span,
                        candidate_node_ids=targets,
                    )
                else:
                    self.add_observation(module_id, "IMPORTS", target_text or text, "missing_dependency", source, span)
            if node.type not in _CALL_TYPES:
                continue
            function = node.child_by_field_name("function") or node.child_by_field_name("name")
            if function is None:
                function = next(iter(node.named_children), None)
            if function is None:
                continue
            text = source.content[function.start_byte:function.end_byte].decode("utf-8", errors="strict")
            name = re.split(r"\.|::|->", text)[-1]
            candidates = tuple(
                item
                for item in self.syntax_definitions.get((language, module_name, name), ())
                if self.nodes[item[1]]["kind"] == "function"
            )
            owners = [
                item for item in functions
                if item[0].start_byte <= node.start_byte and node.end_byte <= item[0].end_byte
            ]
            owner_record = (
                min(owners, key=lambda item: item[0].end_byte - item[0].start_byte)
                if owners else None
            )
            source_node = owner_record[1] if owner_record else module_id
            shadowed = owner_record is not None and self._syntax_shadowed(
                owner_record[0], node, name, source.content
            )
            span = self._syntax_span(node)
            if len(candidates) == 1 and not shadowed and not re.search(r"\.|::|->", text):
                self.add_assertion(source_node, "CALLS", candidates[0][1], source, span, confidence="medium")
            elif len(candidates) > 1 and not shadowed:
                self.add_observation(source_node, "CALLS", text, "ambiguous_target", source, span)
            else:
                reason = "dynamic_dispatch" if re.search(r"\.|::|->", text) else "unresolved_reference"
                self.add_observation(source_node, "CALLS", text, reason, source, span)

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

    def _syntax_type_edges(self, node: object, source: _CapturedSource) -> None:
        language = source.record.language or "unknown"
        module_name = _module_name(source.record.relative_path)
        name = self._syntax_name(node, source.content)
        owners = self.syntax_definitions.get((language, module_name, name or ""), ())
        source_node = next(
            (node_id for candidate, node_id in owners if candidate.start_byte == node.start_byte),
            None,
        )
        if source_node is None:
            return
        declaration = source.content[node.start_byte:node.end_byte].decode("utf-8", errors="strict")
        relationships = (
            ("IMPLEMENTS", r"\bimplements\s+([A-Za-z_]\w*)"),
            ("INHERITS", r"\bextends\s+([A-Za-z_]\w*)"),
        )
        for edge_type, pattern in relationships:
            for match in re.finditer(pattern, declaration):
                targets = self.syntax_definitions.get(
                    (language, module_name, match.group(1)), ()
                )
                if len(targets) != 1:
                    reason = "ambiguous_target" if len(targets) > 1 else "unresolved_reference"
                    start = node.start_byte + match.start(1)
                    end = node.start_byte + match.end(1)
                    self.add_observation(
                        source_node, edge_type, match.group(1), reason, source,
                        (
                            start, end,
                            source.content.count(b"\n", 0, start) + 1,
                            source.content.count(b"\n", 0, end) + 1,
                        ),
                    )
                    continue
                start = node.start_byte + match.start(1)
                end = node.start_byte + match.end(1)
                self.add_assertion(
                    source_node, edge_type, targets[0][1], source,
                    (
                        start, end,
                        source.content.count(b"\n", 0, start) + 1,
                        source.content.count(b"\n", 0, end) + 1,
                    ),
                    confidence="medium",
                )

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
        if len(targets) == 1:
            self.add_assertion(module_id, "IMPORTS", targets[0], source, span)
        elif len(targets) > 1:
            self.add_observation(
                module_id,
                "IMPORTS",
                imported,
                "ambiguous_target",
                source,
                span,
                candidate_node_ids=targets,
            )
        else:
            self.add_observation(module_id, "IMPORTS", imported, "missing_dependency", source, span)

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
        alias = None
        if isinstance(expression, ast.Name):
            alias = aliases.get(expression.id)
        elif isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name):
            alias = aliases.get(expression.value.id)
        if alias is None:
            return ()
        module, symbol = alias
        target_module = f"{module}.{symbol}" if symbol and isinstance(expression, ast.Attribute) else module
        return tuple(self._module_candidates(target_module))

    def _resolve_expression(
        self,
        expression: ast.AST,
        module_name: str,
        aliases: Mapping[str, tuple[str, str]],
        owner: ast.AST | None = None,
    ) -> list[str]:
        if isinstance(expression, ast.Name):
            if expression.id in aliases:
                module, symbol = aliases[expression.id]
                if symbol:
                    return [
                        node_id
                        for candidate in self._matching_modules(module)
                        for node_id in self.definitions.get((candidate, symbol), ())
                    ]
                return self._module_candidates(module)
            if owner is None:
                return list(
                    self.python_scopes.get((module_name, module_name, expression.id), ())
                )
            owner_id = self.node_ast.get(id(owner))
            scope = None if owner_id is None else self.function_body_scope.get(owner_id)
            visited = set()
            while scope is not None and scope not in visited:
                visited.add(scope)
                candidates = self.python_scopes.get(
                    (module_name, scope, expression.id), ()
                )
                if candidates:
                    return list(candidates)
                scope = self.scope_parent.get(scope)
            return list(
                self.python_scopes.get((module_name, module_name, expression.id), ())
            )
        if isinstance(expression, ast.Attribute):
            if isinstance(expression.value, ast.Name) and expression.value.id == "self" and owner:
                owner_id = self.node_ast.get(id(owner))
                owner_name = "" if owner_id is None else str(
                    self.nodes[owner_id]["metadata"].get("owner", "")
                )
                return [
                    node_id for node_id in self.python_scopes.get(
                        (module_name, owner_name, expression.attr), ()
                    )
                    if self.nodes[node_id]["kind"] == "method"
                    and self.nodes[node_id]["metadata"].get("owner") == owner_name
                ]
            if isinstance(expression.value, ast.Name) and expression.value.id in aliases:
                module, symbol = aliases[expression.value.id]
                target_module = f"{module}.{symbol}" if symbol else module
                return [
                    node_id
                    for candidate in self._matching_modules(target_module)
                    for node_id in self.definitions.get((candidate, expression.attr), ())
                ]
            if isinstance(expression.value, ast.Name):
                class_targets = self.definitions.get((module_name, expression.value.id), ())
                return [
                    node_id for node_id in self.python_scopes.get(
                        (module_name, f"{module_name}.{expression.value.id}", expression.attr), ()
                    )
                    if any(self.nodes[node_id]["metadata"].get("owner", "").endswith(expression.value.id) for _ in class_targets)
                ]
        return []

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
            language = source.record.language
            whole = (0, len(source.content), 1, max(1, source.content.count(b"\n") + 1))
            if language != "python":
                parser = _optional_parser(language or "")
                if parser is not None:
                    self.check_stop()
                    tree = parser.parse(source.content)
                    self.check_stop()
                    if tree.root_node.has_error:
                        self.add_observation(
                            self.source_modules[source.record.logical_id],
                            "PARSES", language, "parse_error", source, whole,
                        )
                    else:
                        syntax_trees.append((source, tree.root_node))
                        self.collect_syntax_definitions(source, tree.root_node)
                    continue
                self.add_observation(
                    self.source_modules[source.record.logical_id],
                    "PARSES", language, "unsupported_semantics", source, whole,
                )
                continue
            try:
                self.check_stop()
                tree = ast.parse(source.content, filename=source.record.relative_path)
                self.check_stop()
            except (SyntaxError, ValueError, UnicodeError) as exc:
                self.add_observation(
                    self.source_modules[source.record.logical_id],
                    "PARSES", str(exc), "parse_error", source, whole,
                )
                continue
            parsed.append((source, tree))
            self.collect_python_definitions(source, tree)
        for source, tree in parsed:
            self.check_stop()
            self.collect_python_edges(source, tree)
        for source, root in syntax_trees:
            self.check_stop()
            self.collect_syntax_edges(source, root)
        self.check_stop()
        return CodeExtraction(
            _frozen(list(self.nodes.values()), "node_id"),
            _frozen(self.occurrences, "occurrence_id"),
            _frozen(self.assertions, "assertion_id"),
            _frozen(self.evidence, "evidence_id"),
            _frozen(self.observations, "observation_id"),
            _frozen_observation_dependencies(self.observation_source_dependencies),
        )


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
    if not isinstance(repository_id, str) or not repository_id or len(repository_id) > 512:
        raise ValueError("repository_id must be a bounded non-empty string")
    bounds = limits or ExtractionLimits()
    _check_stop(deadline, cancelled)
    captured_values = []
    for source in sources:
        _check_stop(deadline, cancelled)
        captured_values.append(source)
        if len(captured_values) > bounds.max_sources:
            raise ValueError("code extraction source ceiling exceeded")
    captured = tuple(captured_values)
    required_record_fields = ("logical_id", "relative_path", "sha256", "size", "language")
    if any(
        not hasattr(source, "record")
        or not hasattr(source, "content")
        or any(not hasattr(source.record, field) for field in required_record_fields)
        for source in captured
    ):
        raise TypeError("sources must contain immutable captured source values")
    selected = tuple(sorted(captured, key=lambda item: item.record.relative_path))
    if len({source.record.relative_path for source in selected}) != len(selected):
        raise ValueError("code extraction source paths must be unique")
    if len({source.record.logical_id for source in selected}) != len(selected):
        raise ValueError("code extraction source IDs must be unique")
    total = 0
    for source in selected:
        _check_stop(deadline, cancelled)
        if not isinstance(source.content, bytes):
            raise TypeError("captured source content must be bytes")
        relative = source.record.relative_path
        pure = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            pure is None
            or not relative
            or len(relative) > 4096
            or "\\" in relative
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValueError("captured source path must be canonical and repository-relative")
        total += len(source.content)
        if len(source.content) > bounds.max_source_bytes or total > bounds.max_total_bytes:
            raise ValueError("code extraction source byte ceiling exceeded")
        if source.record.size != len(source.content):
            raise ValueError("captured source size does not match content")
        if source.record.sha256 != hashlib.sha256(source.content).hexdigest():
            raise ValueError("captured source hash does not match content")
    symbols = _bounded_values(
        scip_symbols,
        bounds.max_scip_symbols,
        "SCIP symbol",
        deadline,
        cancelled,
    )
    sources_by_id = {source.record.logical_id: source for source in selected}
    for symbol in symbols:
        _check_stop(deadline, cancelled)
        source = sources_by_id.get(getattr(symbol, "source_id", None))
        if (
            not isinstance(symbol, ScipSymbol)
            or source is None
            or isinstance(symbol.byte_start, bool)
            or not isinstance(symbol.byte_start, int)
            or not isinstance(symbol.byte_end, int)
            or symbol.byte_start < 0
            or symbol.byte_end <= symbol.byte_start
            or symbol.byte_end > len(source.content)
            or not symbol.symbol
            or len(symbol.symbol) > 4096
            or isinstance(symbol.roles, bool)
            or not isinstance(symbol.roles, int)
            or symbol.roles < 0
            or symbol.roles > 0x7F
        ):
            raise ValueError("SCIP symbols must identify a valid captured source span")
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
    if any(
        not isinstance(change, CoChange)
        or not math.isfinite(change.weight)
        or not 0.0 <= change.weight <= 1.0
        or not change.source_path
        or not change.target_path
        or len(change.source_path) > 4096
        or len(change.target_path) > 4096
        for change in changes
    ):
        raise ValueError("co-change records must use bounded finite values")
    if not changes:
        return result
    for change in sorted(changes, key=lambda item: (item.source_path, item.target_path)):
        _check_stop(deadline, cancelled)
        evidence_source = next(
            (source for source in selected if source.record.logical_id == change.evidence_source_id),
            None,
        )
        source_node = collector.files.get(change.source_path)
        target_node = collector.files.get(change.target_path)
        if (
            evidence_source is None
            or source_node is None
            or target_node is None
            or not 0 <= change.byte_start < change.byte_end <= len(evidence_source.content)
            or not 0.0 <= change.weight <= 1.0
        ):
            continue
        line_start = evidence_source.content.count(b"\n", 0, change.byte_start) + 1
        line_end = evidence_source.content.count(b"\n", 0, change.byte_end) + 1
        collector.add_assertion(
            source_node, "CO_CHANGED_WITH", target_node, evidence_source,
            (change.byte_start, change.byte_end, line_start, line_end), confidence="medium",
        )
    return CodeExtraction(
        _frozen(list(collector.nodes.values()), "node_id"),
        _frozen(collector.occurrences, "occurrence_id"),
        _frozen(collector.assertions, "assertion_id"),
        _frozen(collector.evidence, "evidence_id"),
        _frozen(collector.observations, "observation_id"),
        _frozen_observation_dependencies(collector.observation_source_dependencies),
    )


extract_sources = extract_code
