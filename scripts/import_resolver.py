"""Evidence-aware Python import and call resolution."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SymbolRegistry:
    symbols: frozenset[str]
    modules: frozenset[str]


EMPTY_REGISTRY = SymbolRegistry(frozenset(), frozenset())


def build_python_symbol_registry(directory: Path) -> SymbolRegistry:
    """Collect importable Python definitions available in a workspace."""
    symbols: set[str] = set()
    modules: set[str] = set()
    parsed: list[tuple[Path, str, ast.Module]] = []
    for path in sorted(directory.rglob("*.py")):
        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError):
            continue
        module = _module_name(path, directory)
        modules.add(module)
        parsed.append((path, module, tree))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(f"{module}.{node.name}")
            if isinstance(node, ast.ClassDef):
                symbols.update(f"{module}.{node.name}.{name}" for name in _class_methods(node))

    changed = True
    while changed:
        changed = False
        for path, package, tree in parsed:
            if path.name != "__init__.py":
                continue
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom):
                    continue
                source = _absolute_module(package, node)
                for alias in node.names:
                    target = f"{source}.{alias.name}" if source else alias.name
                    exported = f"{package}.{alias.asname or alias.name}"
                    if target in symbols and exported not in symbols:
                        symbols.add(exported)
                        changed = True
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

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for expression in [
            *node.decorator_list,
            *node.args.defaults,
            *(default for default in node.args.kw_defaults if default is not None),
        ]:
            self.visit(expression)
        if node.returns:
            self.visit(node.returns)

        is_method = (
            bool(self.class_stack)
            and len(self.function_stack) == self.class_function_depths[-1]
        )
        class_scope = self.scopes.pop() if is_method else None
        self.function_stack.append(node.name)
        parameters = {
            argument.arg
            for argument in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
        }
        if node.args.vararg:
            parameters.add(node.args.vararg.arg)
        if node.args.kwarg:
            parameters.add(node.args.kwarg.arg)
        static_locals, nonlocals = _function_static_bindings(node)
        nonlocal_imports = {
            name: imported
            for name in nonlocals
            if (imported := self._lookup(0, name)) is not None
        }
        blocked = parameters | static_locals | (nonlocals - nonlocal_imports.keys())
        if class_scope and node.args.args and node.args.args[0].arg == "self":
            blocked.discard("self")
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

    def _visit_body(
        self,
        body: list[ast.stmt],
        blocked: set[str] | None = None,
        semantic_receivers: set[str] | None = None,
        initial_symbols: dict[str, str] | None = None,
    ) -> None:
        symbols: dict[str, str] = dict(initial_symbols or {})
        modules: dict[str, str] = {}
        functions: dict[str, str] = {}
        shadowed = set(blocked or ())
        scope = ".".join([self.module_name, *self.function_stack])
        self.scopes.append((symbols, modules, functions, shadowed))
        self.semantic_receivers.append(set(semantic_receivers or ()))
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[node.name] = f"{scope}.{node.name}"
                symbols.pop(node.name, None)
                modules.pop(node.name, None)
                shadowed.discard(node.name)
                self.visit(node)
            elif isinstance(node, ast.ImportFrom):
                module = self._absolute_import_module(node)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname or alias.name
                    qualified = f"{module}.{alias.name}"
                    self._bind_import(local_name, qualified)
                    self.imports.append(_import_record(node, alias.name, local_name, module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    qualified = alias.name if alias.asname else alias.name.split(".")[0]
                    self._bind_module(local_name, qualified)
                    self.imports.append(_import_record(node, alias.name, local_name, alias.name))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                self.generic_visit(node)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in _assigned_names(target):
                        self._shadow(name)
            else:
                self.visit(node)
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

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_body(node.body)
        for handler in node.handlers:
            if handler.type:
                self.visit(handler.type)
            blocked = {handler.name} if handler.name else set()
            self._visit_body(handler.body, blocked)
        if node.orelse:
            self._visit_body(node.orelse)
        if node.finalbody:
            self._visit_body(node.finalbody)
        statements = [*node.body, *node.orelse, *node.finalbody]
        statements.extend(statement for handler in node.handlers for statement in handler.body)
        self._shadow_bound(statements)

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
            imported = self._lookup(0, func.id)
            if imported:
                return self._result(func.id, imported, "from_import")
            local = self._lookup(2, func.id)
            if local:
                return func.id, local, "confirmed", "local_definition", False, None
            if self._is_shadowed(func.id):
                return func.id, None, "unknown", None, False, "shadowed_binding"
            return func.id, None, "unknown", None, True, "unresolved_callable"

        if isinstance(func, ast.Attribute):
            parts = _attribute_parts(func)
            name = func.attr
            if parts:
                owner = parts[0]
                imported_module = self._lookup(1, owner)
                if imported_module:
                    suffix = ".".join(parts[1:])
                    return self._result(name, f"{imported_module}.{suffix}", "module_import")
                if (
                    len(parts) == 2
                    and owner == "self"
                    and self.class_stack
                    and not self._is_shadowed(owner)
                ):
                    class_name = self.class_stack[-1]
                    if name in self.classes[class_name]:
                        qualified = f"{self.module_name}.{class_name}.{name}"
                        return name, qualified, "confirmed", "self_method", False, None
                if self._is_shadowed(owner):
                    if self._is_semantic_receiver(owner):
                        return name, None, "unknown", None, True, "dynamic_receiver"
                    return name, None, "unknown", None, False, "shadowed_binding"
                if len(parts) == 2 and owner in self.classes and name in self.classes[owner]:
                    qualified = f"{self.module_name}.{owner}.{name}"
                    return name, qualified, "confirmed", "class_method", False, None
            return name, None, "unknown", None, True, "dynamic_receiver"

        return "<anonymous>", None, "unknown", None, False, "unsupported_call_target"

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


def _bound_names(statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(statement.name)
        elif isinstance(statement, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in statement.names if alias.name != "*")
        elif isinstance(statement, ast.Assign):
            names.update(name for target in statement.targets for name in _assigned_names(target))
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            names.update(_assigned_names(statement.target))
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            names.update(_assigned_names(statement.target))
            names.update(_bound_names([*statement.body, *statement.orelse]))
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                if item.optional_vars:
                    names.update(_assigned_names(item.optional_vars))
            names.update(_bound_names(statement.body))
        elif isinstance(statement, ast.If):
            names.update(_bound_names([*statement.body, *statement.orelse]))
        elif isinstance(statement, ast.While):
            names.update(_bound_names([*statement.body, *statement.orelse]))
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                names.update(_match_pattern_names(case.pattern))
                names.update(_bound_names(case.body))
        elif isinstance(statement, ast.Try):
            names.update(_bound_names([*statement.body, *statement.orelse, *statement.finalbody]))
            for handler in statement.handlers:
                if handler.name:
                    names.add(handler.name)
                names.update(_bound_names(handler.body))
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


def _function_static_bindings(node: ast.FunctionDef) -> tuple[set[str], set[str]]:
    collector = _FunctionBindingCollector()
    for statement in node.body:
        collector.visit(statement)
    local = collector.bound - collector.globals - collector.nonlocals
    return local, collector.nonlocals


def _absolute_module(package_name: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = package_name.split(".") if package_name else []
    if node.level > 1:
        package = package[: -(node.level - 1)]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _match_pattern_names(pattern: ast.pattern) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return names
