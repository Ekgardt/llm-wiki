"""Evidence-aware Python import and call resolution."""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SymbolRegistry:
    symbols: frozenset[str]
    modules: frozenset[str]


EMPTY_REGISTRY = SymbolRegistry(frozenset(), frozenset())


# The one directory rule the vault's walkers already agree on (NEW-110): the
# corpus walker prunes every hidden directory (`corpus_snapshot.
# _directory_excluded`), which is why the graph index never indexes `.claude`
# agent worktrees, and `code_graph._WORKSPACE_SKIP_PARTS` names the workspace
# caches. Without this rule the registry walked 7,549 files on the live vault
# (7,215 under `.claude/`) and died of MemoryError.
_SKIPPED_DIRECTORY_NAMES = frozenset({"node_modules", "venv", "__pycache__"})


def _directory_skipped(name: str) -> bool:
    return name.startswith(".") or name in _SKIPPED_DIRECTORY_NAMES


def _kept_subdirectories(directories: list[str]) -> list[str]:
    return sorted(name for name in directories if not _directory_skipped(name))


def _python_files_in(parent: Path, files: list[str]) -> list[Path]:
    return [parent / name for name in sorted(files) if name.endswith(".py")]


def _workspace_python_files(directory: Path) -> list[Path]:
    """Python files below the root, pruning skipped directories, not the root."""
    collected: list[Path] = []
    for current, directories, files in os.walk(directory):
        directories[:] = _kept_subdirectories(directories)
        collected.extend(_python_files_in(Path(current), files))
    return collected


@dataclass(frozen=True)
class _ReExport:
    """One `from x import y` line of an `__init__.py`, kept instead of its AST."""

    package: str
    source: str
    exported: str


def _parsed_module(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        return None


def _module_symbols(module: str, tree: ast.Module) -> set[str]:
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(f"{module}.{node.name}")
        if isinstance(node, ast.ClassDef):
            symbols.update(f"{module}.{node.name}.{name}" for name in _class_methods(node))
    return symbols


def _package_reexports(path: Path, package: str, tree: ast.Module) -> list[_ReExport]:
    if path.name != "__init__.py":
        return []
    records: list[_ReExport] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            records.extend(_reexport_records(package, node))
    return records


def _reexport_records(package: str, node: ast.ImportFrom) -> list[_ReExport]:
    source = _absolute_module(package, node)
    records = []
    for alias in node.names:
        target = f"{source}.{alias.name}" if source else alias.name
        records.append(_ReExport(package, target, alias.asname or alias.name))
    return records


def _added_exports(symbols: set[str], reexports: list[_ReExport]) -> bool:
    changed = False
    for record in reexports:
        exported = f"{record.package}.{record.exported}"
        if record.source in symbols and exported not in symbols:
            symbols.add(exported)
            changed = True
    return changed


def _resolve_reexports(symbols: set[str], reexports: list[_ReExport]) -> None:
    """Monotone fixed point: each round only adds, so it always terminates."""
    while _added_exports(symbols, reexports):
        pass


def build_python_symbol_registry(directory: Path) -> SymbolRegistry:
    """Collect importable Python definitions available in a workspace.

    Each file's AST lives only for its own extraction pass; the re-export
    resolution afterwards runs on `_ReExport` records (NEW-110 kept every
    tree alive at once, ~1.5 GiB RSS per 1,000 files).
    """
    symbols: set[str] = set()
    modules: set[str] = set()
    reexports: list[_ReExport] = []
    for path in _workspace_python_files(directory):
        tree = _parsed_module(path)
        if tree is None:
            continue
        module = _module_name(path, directory)
        modules.add(module)
        symbols.update(_module_symbols(module, tree))
        reexports.extend(_package_reexports(path, module, tree))
    _resolve_reexports(symbols, reexports)
    return SymbolRegistry(frozenset(symbols), frozenset(modules))


def resolve_python_imports_and_calls(
    file_path: Path,
    workspace_symbols: SymbolRegistry | None = None,
    workspace_root: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return Python imports and calls with lexical and workspace evidence."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        return [], []

    root = workspace_root or file_path.parent
    module_name = _module_name(file_path, root)
    package_name = module_name if file_path.name == "__init__.py" else module_name.rpartition(".")[0]
    visitor = _CallVisitor(module_name, package_name, workspace_symbols or EMPTY_REGISTRY)
    visitor.visit(tree)
    return visitor.imports, visitor.calls


def _module_name(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).with_suffix("")
    except ValueError:
        return path.stem
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.parent.name


def _class_methods(node: ast.ClassDef) -> set[str]:
    return {
        child.name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class _CallVisitor(ast.NodeVisitor):
    def __init__(
        self, module_name: str, package_name: str, workspace_symbols: SymbolRegistry
    ) -> None:
        self.module_name = module_name
        self.package_name = package_name
        self.workspace_symbols = workspace_symbols
        self.scopes: list[
            tuple[dict[str, str], dict[str, str], dict[str, str], set[str]]
        ] = []
        self.semantic_receivers: list[set[str]] = []
        self.class_stack: list[str] = []
        self.class_function_depths: list[int] = []
        self.function_stack: list[str] = []
        self.classes: dict[str, set[str]] = {}
        self.imports: list[dict] = []
        self.calls: list[dict] = []

    def visit_Module(self, node: ast.Module) -> None:
        self._visit_body(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in [*node.decorator_list, *node.bases]:
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.classes[node.name] = _class_methods(node)
        self.class_stack.append(node.name)
        self.class_function_depths.append(len(self.function_stack))
        self._visit_body(node.body)
        self.class_function_depths.pop()
        self.class_stack.pop()

    def _visit_function_header(self, node: ast.FunctionDef) -> None:
        for expression in [
            *node.decorator_list,
            *node.args.defaults,
            *(default for default in node.args.kw_defaults if default is not None),
        ]:
            self.visit(expression)
        if node.returns:
            self.visit(node.returns)

    def _is_direct_method(self) -> bool:
        return (
            bool(self.class_stack)
            and len(self.function_stack) == self.class_function_depths[-1]
        )

    @staticmethod
    def _keeps_self(node: ast.FunctionDef, class_scope: object) -> bool:
        arguments = node.args.args
        return bool(class_scope) and bool(arguments) and arguments[0].arg == "self"

    def _function_bindings(
        self, node: ast.FunctionDef, parameters: set[str], class_scope: object
    ) -> tuple[dict[str, str], set[str]]:
        static_locals, nonlocals = _function_static_bindings(node)
        nonlocal_imports = {
            name: imported
            for name in nonlocals
            if (imported := self._lookup(0, name)) is not None
        }
        blocked = parameters | static_locals | (nonlocals - nonlocal_imports.keys())
        if self._keeps_self(node, class_scope):
            blocked.discard("self")
        return nonlocal_imports, blocked

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_header(node)
        class_scope = self.scopes.pop() if self._is_direct_method() else None
        self.function_stack.append(node.name)
        parameters = _function_parameters(node.args)
        nonlocal_imports, blocked = self._function_bindings(node, parameters, class_scope)
        self._visit_body(
            node.body,
            blocked,
            parameters - {"self"},
            initial_symbols=nonlocal_imports,
        )
        self.function_stack.pop()
        if class_scope:
            self.scopes.append(class_scope)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _enter_local_function(self, node: ast.stmt, scope: str) -> None:
        symbols, modules, functions, shadowed = self.scopes[-1]
        functions[node.name] = f"{scope}.{node.name}"
        symbols.pop(node.name, None)
        modules.pop(node.name, None)
        shadowed.discard(node.name)
        self.visit(node)

    def _record_from_alias(self, node: ast.ImportFrom, alias: ast.alias, module: str) -> None:
        if alias.name == "*":
            return
        local_name = alias.asname or alias.name
        self._bind_import(local_name, f"{module}.{alias.name}")
        self.imports.append(_import_record(node, alias.name, local_name, module))

    def _bind_from_import(self, node: ast.ImportFrom, scope: str) -> None:
        module = self._absolute_import_module(node)
        for alias in node.names:
            self._record_from_alias(node, alias, module)

    def _bind_plain_import(self, node: ast.Import, scope: str) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".")[0]
            qualified = alias.name if alias.asname else alias.name.split(".")[0]
            self._bind_module(local_name, qualified)
            self.imports.append(_import_record(node, alias.name, local_name, alias.name))

    def _track_assignment(self, node: ast.stmt, scope: str) -> None:
        self.generic_visit(node)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            self._shadow_assigned(target)

    def _shadow_assigned(self, target: ast.expr) -> None:
        for name in _assigned_names(target):
            self._shadow(name)

    _STATEMENT_DRIVERS = (
        ((ast.FunctionDef, ast.AsyncFunctionDef), _enter_local_function),
        ((ast.ImportFrom,), _bind_from_import),
        ((ast.Import,), _bind_plain_import),
        ((ast.Assign, ast.AnnAssign, ast.AugAssign), _track_assignment),
    )

    def _visit_statement(self, node: ast.stmt, scope: str) -> None:
        for types, driver in self._STATEMENT_DRIVERS:
            if isinstance(node, types):
                driver(self, node, scope)
                return
        self.visit(node)

    def _visit_body(
        self,
        body: list[ast.stmt],
        blocked: set[str] | None = None,
        semantic_receivers: set[str] | None = None,
        initial_symbols: dict[str, str] | None = None,
    ) -> None:
        scope = ".".join([self.module_name, *self.function_stack])
        self.scopes.append((dict(initial_symbols or {}), {}, {}, set(blocked or ())))
        self.semantic_receivers.append(set(semantic_receivers or ()))
        for node in body:
            self._visit_statement(node, scope)
        self.scopes.pop()
        self.semantic_receivers.pop()

    def _absolute_import_module(self, node: ast.ImportFrom) -> str:
        return _absolute_module(self.package_name, node)

    def _bind_import(self, name: str, qualified: str) -> None:
        symbols, modules, functions, shadowed = self.scopes[-1]
        functions.pop(name, None)
        shadowed.discard(name)
        if qualified in self.workspace_symbols.modules:
            modules[name] = qualified
            symbols.pop(name, None)
        else:
            symbols[name] = qualified
            modules.pop(name, None)

    def _bind_module(self, name: str, qualified: str) -> None:
        symbols, modules, functions, shadowed = self.scopes[-1]
        symbols.pop(name, None)
        functions.pop(name, None)
        shadowed.discard(name)
        modules[name] = qualified

    def _shadow(self, name: str) -> None:
        symbols, modules, functions, shadowed = self.scopes[-1]
        symbols.pop(name, None)
        modules.pop(name, None)
        functions.pop(name, None)
        shadowed.add(name)
        self.semantic_receivers[-1].discard(name)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_body(node.body)
        if node.orelse:
            self._visit_body(node.orelse)
        self._shadow_bound(node.body + node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_body(node.body)
        if node.orelse:
            self._visit_body(node.orelse)
        self._shadow_bound([*node.body, *node.orelse])

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        pattern_names: set[str] = set()
        statements: list[ast.stmt] = []
        for case in node.cases:
            bound = _match_pattern_names(case.pattern)
            pattern_names.update(bound)
            if case.guard:
                self._visit_expressions([case.guard], bound)
            self._visit_body(case.body, bound)
            statements.extend(case.body)
        self._shadow_bound(statements, pattern_names)

    def _visit_expressions(self, expressions: list[ast.expr], blocked: set[str]) -> None:
        self.scopes.append(({}, {}, {}, set(blocked)))
        for expression in expressions:
            self.visit(expression)
        self.scopes.pop()

    def _visit_handler(self, handler: ast.ExceptHandler) -> None:
        if handler.type:
            self.visit(handler.type)
        blocked = {handler.name} if handler.name else set()
        self._visit_body(handler.body, blocked)

    @staticmethod
    def _try_statements(node: ast.Try) -> list[ast.stmt]:
        statements = [*node.body, *node.orelse, *node.finalbody]
        statements.extend(statement for handler in node.handlers for statement in handler.body)
        return statements

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_body(node.body)
        for handler in node.handlers:
            self._visit_handler(handler)
        if node.orelse:
            self._visit_body(node.orelse)
        if node.finalbody:
            self._visit_body(node.finalbody)
        self._shadow_bound(self._try_statements(node))

    visit_TryStar = visit_Try

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        blocked = _assigned_names(node.target)
        self._visit_body(node.body, blocked)
        if node.orelse:
            self._visit_body(node.orelse)
        self._shadow_bound([*node.body, *node.orelse], blocked)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        blocked: set[str] = set()
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                blocked.update(_assigned_names(item.optional_vars))
        self._visit_body(node.body, blocked)
        self._shadow_bound(node.body, blocked)

    visit_AsyncWith = visit_With

    def _shadow_bound(self, statements: list[ast.stmt], names: set[str] | None = None) -> None:
        for name in set(names or ()) | _bound_names(statements):
            self._shadow(name)

    def visit_Call(self, node: ast.Call) -> None:
        (
            name,
            qualified_name,
            confidence,
            evidence,
            semantic_eligible,
            unresolved_reason,
        ) = self._resolve(node.func)
        self.calls.append({
            "name": name,
            "qualified_name": qualified_name,
            "confidence": confidence,
            "evidence": evidence,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "column": max(0, getattr(node.func, "end_col_offset", node.col_offset) - len(name)),
            "end_column": getattr(node, "end_col_offset", node.col_offset),
            "semantic_eligible": semantic_eligible,
            "unresolved_reason": unresolved_reason,
        })
        self.generic_visit(node)

    def _lookup(self, index: int, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope[3]:
                return None
            if name in scope[index]:
                return scope[index][name]
        return None

    def _resolve(
        self, func: ast.expr
    ) -> tuple[str, str | None, str, str | None, bool, str | None]:
        if isinstance(func, ast.Name):
            return self._resolve_name(func.id)
        if isinstance(func, ast.Attribute):
            return self._resolve_attribute(func)
        return "<anonymous>", None, "unknown", None, False, "unsupported_call_target"

    def _resolved_name_binding(
        self, name: str
    ) -> tuple[str, str | None, str, str | None, bool, str | None] | None:
        imported = self._lookup(0, name)
        if imported:
            return self._result(name, imported, "from_import")
        local = self._lookup(2, name)
        if local:
            return name, local, "confirmed", "local_definition", False, None
        return None

    def _resolve_name(
        self, name: str
    ) -> tuple[str, str | None, str, str | None, bool, str | None]:
        resolved = self._resolved_name_binding(name)
        if resolved is not None:
            return resolved
        if self._is_shadowed(name):
            return name, None, "unknown", None, False, "shadowed_binding"
        return name, None, "unknown", None, True, "unresolved_callable"

    def _resolve_attribute(
        self, func: ast.Attribute
    ) -> tuple[str, str | None, str, str | None, bool, str | None]:
        name = func.attr
        resolved = self._resolved_attribute_owner(name, _attribute_parts(func))
        if resolved is not None:
            return resolved
        return name, None, "unknown", None, True, "dynamic_receiver"

    def _resolved_attribute_owner(
        self, name: str, parts: list[str]
    ) -> tuple[str, str | None, str, str | None, bool, str | None] | None:
        if not parts:
            return None
        owner = parts[0]
        imported_module = self._lookup(1, owner)
        if imported_module:
            suffix = ".".join(parts[1:])
            return self._result(name, f"{imported_module}.{suffix}", "module_import")
        return self._resolved_receiver(name, owner, parts)

    def _resolved_receiver(
        self, name: str, owner: str, parts: list[str]
    ) -> tuple[str, str | None, str, str | None, bool, str | None] | None:
        method = self._resolved_self_method(name, owner, parts)
        if method is not None:
            return method
        if self._is_shadowed(owner):
            return self._shadowed_receiver(name, owner)
        return self._resolved_class_method(name, owner, parts)

    def _self_receiver(self, owner: str, parts: list[str]) -> bool:
        return (
            len(parts) == 2
            and owner == "self"
            and bool(self.class_stack)
            and not self._is_shadowed(owner)
        )

    def _resolved_self_method(
        self, name: str, owner: str, parts: list[str]
    ) -> tuple[str, str | None, str, str | None, bool, str | None] | None:
        if not self._self_receiver(owner, parts):
            return None
        class_name = self.class_stack[-1]
        if name not in self.classes[class_name]:
            return None
        qualified = f"{self.module_name}.{class_name}.{name}"
        return name, qualified, "confirmed", "self_method", False, None

    def _shadowed_receiver(
        self, name: str, owner: str
    ) -> tuple[str, str | None, str, str | None, bool, str | None]:
        if self._is_semantic_receiver(owner):
            return name, None, "unknown", None, True, "dynamic_receiver"
        return name, None, "unknown", None, False, "shadowed_binding"

    def _resolved_class_method(
        self, name: str, owner: str, parts: list[str]
    ) -> tuple[str, str | None, str, str | None, bool, str | None] | None:
        if len(parts) == 2 and owner in self.classes and name in self.classes[owner]:
            qualified = f"{self.module_name}.{owner}.{name}"
            return name, qualified, "confirmed", "class_method", False, None
        return None

    def _result(
        self, name: str, qualified: str, evidence: str
    ) -> tuple[str, str, str, str, bool, str | None]:
        confidence = "confirmed" if qualified in self.workspace_symbols.symbols else "unknown"
        reason = None if confidence == "confirmed" else "missing_import_target"
        return name, qualified, confidence, evidence, False, reason

    def _is_shadowed(self, name: str) -> bool:
        return any(name in scope[3] for scope in reversed(self.scopes))

    def _is_semantic_receiver(self, name: str) -> bool:
        return any(name in receivers for receivers in reversed(self.semantic_receivers))


def _import_record(node: ast.AST, name: str, local_name: str, module: str) -> dict:
    return {
        "name": name,
        "local_name": local_name,
        "module": module,
        "line": node.lineno,
        "end_line": getattr(node, "end_lineno", node.lineno),
    }


def _assigned_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for child in node.elts for name in _assigned_names(child)}
    return set()


def _attribute_parts(node: ast.expr) -> list[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return []
    parts.append(node.id)
    return list(reversed(parts))


def _names_from_definition(statement: ast.stmt, names: set[str]) -> None:
    names.add(statement.name)


def _names_from_import(statement: ast.Import, names: set[str]) -> None:
    names.update(alias.asname or alias.name.split(".")[0] for alias in statement.names)


def _names_from_import_from(statement: ast.ImportFrom, names: set[str]) -> None:
    names.update(alias.asname or alias.name for alias in statement.names if alias.name != "*")


def _names_from_assign(statement: ast.Assign, names: set[str]) -> None:
    names.update(name for target in statement.targets for name in _assigned_names(target))


def _names_from_target(statement: ast.stmt, names: set[str]) -> None:
    names.update(_assigned_names(statement.target))


def _names_from_loop(statement: ast.stmt, names: set[str]) -> None:
    names.update(_assigned_names(statement.target))
    names.update(_bound_names([*statement.body, *statement.orelse]))


def _names_from_with(statement: ast.stmt, names: set[str]) -> None:
    for item in statement.items:
        if item.optional_vars:
            names.update(_assigned_names(item.optional_vars))
    names.update(_bound_names(statement.body))


def _names_from_branch(statement: ast.stmt, names: set[str]) -> None:
    names.update(_bound_names([*statement.body, *statement.orelse]))


def _names_from_match(statement: ast.Match, names: set[str]) -> None:
    for case in statement.cases:
        names.update(_match_pattern_names(case.pattern))
        names.update(_bound_names(case.body))


def _names_from_handler(handler: ast.ExceptHandler, names: set[str]) -> None:
    if handler.name:
        names.add(handler.name)
    names.update(_bound_names(handler.body))


def _names_from_try(statement: ast.Try, names: set[str]) -> None:
    names.update(_bound_names([*statement.body, *statement.orelse, *statement.finalbody]))
    for handler in statement.handlers:
        _names_from_handler(handler, names)


_BOUND_NAME_EXTRACTORS = (
    ((ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef), _names_from_definition),
    ((ast.Import,), _names_from_import),
    ((ast.ImportFrom,), _names_from_import_from),
    ((ast.Assign,), _names_from_assign),
    ((ast.AnnAssign, ast.AugAssign), _names_from_target),
    ((ast.For, ast.AsyncFor), _names_from_loop),
    ((ast.With, ast.AsyncWith), _names_from_with),
    ((ast.If, ast.While), _names_from_branch),
    ((ast.Match,), _names_from_match),
    ((ast.Try,), _names_from_try),
)


def _collect_bound(statement: ast.stmt, names: set[str]) -> None:
    for types, extractor in _BOUND_NAME_EXTRACTORS:
        if isinstance(statement, types):
            extractor(statement, names)
            return


def _bound_names(statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        _collect_bound(statement, names)
    return names


class _FunctionBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.bound.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.bound.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocals.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)


def _function_parameters(args: ast.arguments) -> set[str]:
    parameters = {
        argument.arg
        for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]
    }
    if args.vararg:
        parameters.add(args.vararg.arg)
    if args.kwarg:
        parameters.add(args.kwarg.arg)
    return parameters


def _function_static_bindings(node: ast.FunctionDef) -> tuple[set[str], set[str]]:
    collector = _FunctionBindingCollector()
    for statement in node.body:
        collector.visit(statement)
    local = collector.bound - collector.globals - collector.nonlocals
    return local, collector.nonlocals


def _relative_package(package_name: str, level: int) -> list[str]:
    package = package_name.split(".") if package_name else []
    if level > 1:
        return package[: -(level - 1)]
    return package


def _absolute_module(package_name: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = _relative_package(package_name, node.level)
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _pattern_binding(node: ast.AST) -> str | None:
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def _match_pattern_names(pattern: ast.pattern) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(pattern):
        name = _pattern_binding(node)
        if name:
            names.add(name)
    return names
