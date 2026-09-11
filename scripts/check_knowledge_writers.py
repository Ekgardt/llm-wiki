"""Find direct writes that can target authoritative knowledge files."""

from __future__ import annotations

import argparse
import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

EXECUTABLE_SUFFIXES = {".py", ".js", ".ps1", ".sh"}
SEARCH_DIRS = ("scripts", "integrations")
_COVERED_RE = re.compile(
    r"(?:^|/)knowledge/(?:daily|notes|projects|inbox|feedback)(?:/|$)|"
    r"(?:^|/)knowledge/(?:guardrails|index|log)\.md$",
    re.IGNORECASE,
)
_BOUNDARIES = {
    "mutate_knowledge", "append_knowledge", "locked_append", "locked_append_once",
    "ensure_target_parent",
}
_PATH_METHODS = {"open", "write_text", "write_bytes", "touch", "unlink", "mkdir"}
_RENAME_METHODS = {"replace", "rename", "move"}
_ARCHIVE_BUILD_FUNCTIONS = {
    "_build_bag_contents", "_prepare_build_for_publish", "_recover_hidden_builds",
    "_publish_build",
}


@dataclass(frozen=True)
class WriterFinding:
    path: Path
    line: int
    api: str
    approved: bool = False
    function: str = "<module>"

    def __str__(self) -> str:
        status = "approved" if self.approved else "DIRECT COVERED WRITE"
        return f"{self.path.as_posix()}:{self.line}: {status}: {self.api}"


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _covered(value: str) -> bool:
    return bool(_COVERED_RE.search(value.replace("\\", "/").replace("<root>/", "")))


_TRANSACTION_MODULES = frozenset({"markdown_transaction", "scripts.markdown_transaction"})
_DAILY_APPENDERS = frozenset({"locked_append", "locked_append_once", "append_daily"})
_APPLY_FUNCTIONS = frozenset({"_apply_operation", "_apply_windows_operation"})
_APPLY_WRITES = frozenset({"unlink", "replace", "durable_publish_file", "_delete_windows_handle"})
_ARCHIVE_OPERATIONS = frozenset({"write_bytes", "unlink", "replace", "rename", "move", "mkdir"})
_ARCHIVE_PUBLISHERS = frozenset({"_recover_hidden_builds", "_publish_build"})
_HIDDEN_BUILD_WRITES = frozenset({"write_bytes", "unlink", "mkdir"})
_BUILD_PARAMETERS = frozenset({"build", "publish_build", "hidden_build"})
_PATH_DERIVATIONS = frozenset({"absolute", "resolve", "with_name", "with_suffix"})
_PARAMETER_RE = re.compile(r"<param:(\d+)>")
Env = dict[str, set[str]]


class _ModuleBindings:
    """Names a module binds to the knowledge boundaries, found anywhere in it."""

    def __init__(self) -> None:
        self.canonical: dict[str, str] = {}
        self.path_aliases = {"Path"}
        self.aliases: dict[str, str] = {}
        self.canonical_modules: set[str] = set()
        self.handlers = {
            ast.ImportFrom: self._import_from,
            ast.Import: self._import,
            ast.Assign: self._assign,
        }

    def observe(self, statement: ast.AST) -> None:
        handler = self.handlers.get(type(statement))
        if handler is not None:
            handler(statement)

    def _import_from(self, statement: ast.ImportFrom) -> None:
        for item in statement.names:
            self._imported(statement.module, item)

    def _imported(self, module: str | None, item: ast.alias) -> None:
        local = item.asname or item.name
        if _canonical_import(module, item.name):
            self.canonical[local] = item.name
            return
        if module == "pathlib" and item.name == "Path":
            self.path_aliases.add(local)

    def _import(self, statement: ast.Import) -> None:
        for item in statement.names:
            if item.name in _TRANSACTION_MODULES:
                self.canonical_modules.add(item.asname or item.name)

    def _assign(self, statement: ast.Assign) -> None:
        names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        if isinstance(statement.value, ast.Name):
            self._alias_names(names, statement.value.id)
            return
        if _module_boundary(statement.value, self.canonical_modules):
            self._bind_boundary(names, statement.value.attr)

    def _alias_names(self, names: list[str], source: str) -> None:
        for name in names:
            self.aliases[name] = source
            if source in self.canonical:
                self.canonical[name] = self.canonical[source]

    def _bind_boundary(self, names: list[str], boundary: str) -> None:
        for name in names:
            self.aliases[name] = boundary
            self.canonical[name] = boundary


def _canonical_import(module: str | None, name: str) -> bool:
    if module in _TRANSACTION_MODULES:
        return name in _BOUNDARIES
    return module == "daily_log_append" and name in _DAILY_APPENDERS


def _module_boundary(value: ast.AST, canonical_modules: set[str]) -> bool:
    """`module.boundary` where the module is an imported transaction module."""
    if not isinstance(value, ast.Attribute) or not isinstance(value.value, ast.Name):
        return False
    return value.value.id in canonical_modules and value.attr in _BOUNDARIES


def _python_bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], set[str], dict[str, str], set[str]]:
    bindings = _ModuleBindings()
    for statement in ast.walk(tree):
        bindings.observe(statement)
    for statement in _functions_in_body(tree):
        bindings.canonical.pop(statement.name, None)
    return bindings.canonical, bindings.path_aliases, bindings.aliases, bindings.canonical_modules


def _functions_in_body(tree: ast.Module) -> list[ast.AST]:
    return [
        statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _copy_env(env: Env) -> Env:
    return {name: set(items) for name, items in env.items()}


def _merge(*environments: Env) -> Env:
    return {
        name: set().union(*(env.get(name, set()) for env in environments))
        for name in set().union(*(set(env) for env in environments))
    }


def _value(node: ast.AST | None, env: Env) -> set[str]:
    """The path strings (and binding markers) an expression can evaluate to."""
    if node is None:
        return set()
    evaluator = _VALUE_EVALUATORS.get(type(node))
    if evaluator is None:
        return set()
    return evaluator(node, env)


def _constant_value(node: ast.Constant, env: Env) -> set[str]:
    if isinstance(node.value, str):
        return {node.value.replace("\\", "/")}
    return set()


def _name_value(node: ast.Name, env: Env) -> set[str]:
    return set(env.get(node.id, set()))


def _formatted_value(node: ast.FormattedValue, env: Env) -> set[str]:
    return {"<value>"}


def _joined_value(node: ast.JoinedStr, env: Env) -> set[str]:
    result = {""}
    for item in node.values:
        parts = _value(item, env) or {""}
        result = {left + right for left in result for right in parts}
    return result


def _binop_value(node: ast.BinOp, env: Env) -> set[str]:
    if not isinstance(node.op, (ast.Div, ast.Add)):
        return set()
    left, right = _value(node.left, env), _value(node.right, env)
    return _joined_paths(left, right, "/" if isinstance(node.op, ast.Div) else "")


def _joined_paths(left: set[str], right: set[str], separator: str) -> set[str]:
    if not left:
        return right
    if not right:
        return left
    return {f"{a}{separator}{b}" for a in left for b in right}


def _call_value(node: ast.Call, env: Env) -> set[str]:
    constructors = _value(node.func, env)
    if "<path-constructor>" in constructors and node.args:
        return _value(node.args[0], env)
    if "<coordinator-class>" in constructors:
        return {"<coordinator-instance>"}
    return _derived_path_value(node, env)


def _derived_path_value(node: ast.Call, env: Env) -> set[str]:
    if isinstance(node.func, ast.Attribute) and node.func.attr in _PATH_DERIVATIONS:
        return _value(node.func.value, env)
    return set()


def _attribute_value(node: ast.Attribute, env: Env) -> set[str]:
    base = _value(node.value, env)
    if "<canonical-module>" in base and node.attr in _BOUNDARIES:
        return {f"<canonical:{node.attr}>"}
    return {f"{item}/{node.attr}" for item in base} or set(env.get(node.attr, set()))


_VALUE_EVALUATORS = {
    ast.Constant: _constant_value,
    ast.Name: _name_value,
    ast.FormattedValue: _formatted_value,
    ast.JoinedStr: _joined_value,
    ast.BinOp: _binop_value,
    ast.Call: _call_value,
    ast.Attribute: _attribute_value,
}


def _call_bindings(call: ast.Call, env: Env) -> set[str]:
    if isinstance(call.func, ast.Name):
        return set(env.get(call.func.id, set())) or {f"<function:{call.func.id}>"}
    if isinstance(call.func, ast.Attribute):
        return _method_bindings(call.func, env)
    return set()


def _method_bindings(func: ast.Attribute, env: Env) -> set[str]:
    modules = _value(func.value, env)
    if "<canonical-module>" in modules and func.attr in _BOUNDARIES:
        return {f"<canonical:{func.attr}>"}
    if "<coordinator-instance>" in modules and func.attr == "ensure_target_parent":
        return {"<canonical:ensure_target_parent>"}
    return {f"<function:{func.attr}>"}


def _tagged(bindings: set[str], tag: str) -> set[str]:
    prefix = f"<{tag}:"
    return {item.removeprefix(prefix).removesuffix(">") for item in bindings if item.startswith(prefix)}


def _summaries_approved(summaries: list[list[tuple[int, str, bool]]]) -> bool:
    return all(item[2] for summary in summaries for item in summary)


def _reported_name(function_names: set[str], name: str) -> str:
    if len(function_names) == 1:
        return next(iter(function_names))
    return name


def _summary_argument_values(call: ast.Call, env: Env, summaries: list[list]) -> set[str]:
    arguments = [
        call.args[index]
        for summary in summaries
        for index, _, _ in summary
        if len(call.args) > index
    ]
    return set().union(*(_value(argument, env) for argument in arguments))


def _apparent_boundary(boundary: str | None, name: str) -> str | None:
    if boundary is not None:
        return boundary
    if name in _BOUNDARIES:
        return name
    return None


def _direct_targets(
    call: ast.Call, env: Env, name: str, boundary: str | None
) -> tuple[list[str], bool, str]:
    apparent = _apparent_boundary(boundary, name)
    if apparent:
        return sorted(_boundary_targets(call, env, apparent)), boundary is not None, name
    return sorted(_plain_write_targets(call, env, name)), False, name


def _boundary_targets(call: ast.Call, env: Env, boundary: str) -> set[str]:
    if boundary == "ensure_target_parent":
        return _first_argument_value(call, env)
    if boundary == "mutate_knowledge" and _has_change_mapping(call):
        return _mapping_key_values(call.args[1], env)
    return _positional_boundary_target(call, env, boundary)


def _mapping_key_values(mapping: ast.Dict, env: Env) -> set[str]:
    return {item for key in mapping.keys for item in _value(key, env)}


def _has_change_mapping(call: ast.Call) -> bool:
    return len(call.args) > 1 and isinstance(call.args[1], ast.Dict)


def _positional_boundary_target(call: ast.Call, env: Env, boundary: str) -> set[str]:
    if boundary == "append_knowledge" and len(call.args) == 2:
        return _value(call.args[0], env)
    index = 1 if boundary == "append_knowledge" else 0
    if len(call.args) > index:
        return _value(call.args[index], env)
    return set()


def _first_argument_value(call: ast.Call, env: Env) -> set[str]:
    if not call.args:
        return set()
    return _value(call.args[0], env)


def _plain_write_targets(call: ast.Call, env: Env, name: str) -> set[str]:
    handler = _PLAIN_WRITERS.get(name)
    if handler is not None:
        return handler(call, env)
    if name in _PATH_METHODS:
        return _receiver_value(call, env)
    return set()


def _receiver_value(call: ast.Call, env: Env) -> set[str]:
    if isinstance(call.func, ast.Attribute):
        return _value(call.func.value, env)
    return set()


def _writes(modes: set[str]) -> bool:
    return any(any(flag in mode for flag in "wax+") for mode in modes)


def _open_targets(call: ast.Call, env: Env) -> set[str]:
    if isinstance(call.func, ast.Attribute):
        return _method_open_targets(call, env)
    modes = _value(call.args[1], env) if len(call.args) > 1 else {"r"}
    if call.args and _writes(modes):
        return _value(call.args[0], env)
    return set()


def _method_open_targets(call: ast.Call, env: Env) -> set[str]:
    modes = _value(call.args[0], env) if call.args else {"r"}
    if _writes(modes):
        return _value(call.func.value, env)
    return set()


def _rename_targets(call: ast.Call, env: Env) -> set[str]:
    if not isinstance(call.func, ast.Attribute):
        return set()
    return _attribute_rename_targets(call, env, _receiver_name(call.func))


def _receiver_name(func: ast.Attribute) -> str | None:
    if isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _attribute_rename_targets(call: ast.Call, env: Env, receiver: str | None) -> set[str]:
    if receiver == "MarkdownChange":
        return set()
    if receiver in {"os", "shutil"}:
        return {value_ for item in call.args[:2] for value_ in _value(item, env)}
    result = _value(call.func.value, env)
    result.update(_first_argument_value(call, env))
    return result


_PLAIN_WRITERS = {
    "open": _open_targets,
    "atomic_write": _first_argument_value,
    **{name: _rename_targets for name in _RENAME_METHODS},
}


def _archive_operation(function: str, name: str) -> bool:
    return function in _ARCHIVE_BUILD_FUNCTIONS and name in _ARCHIVE_OPERATIONS


def _archive_targets(resolved: list[str], function: str, archive_operation: bool) -> list[str]:
    if archive_operation and not resolved and function in _ARCHIVE_PUBLISHERS:
        return [".bag-building"]
    return resolved


def _archive_allowed(resolved: list[str], function: str, name: str) -> bool:
    hidden = _hidden_build_targets(resolved)
    return (hidden and name in _HIDDEN_BUILD_WRITES) or (
        function in _ARCHIVE_PUBLISHERS and name in _RENAME_METHODS
    )


def _hidden_build_targets(resolved: list[str]) -> bool:
    return any(".bag" in item or ".building" in item for item in resolved)


def _reportable(resolved: list[str], archive_write: bool) -> bool:
    if not resolved:
        return False
    return any(_covered(item) for item in resolved) or archive_write


def _finding_approval(
    function: str, binding_approved: bool, archive_operation: bool, archive_allowed: bool
) -> bool:
    if archive_operation:
        return archive_allowed
    if function in _APPLY_FUNCTIONS:
        return True
    return binding_approved


def _parameter_indexes(resolved: list[str]) -> list[int]:
    matches = (_PARAMETER_RE.fullmatch(target) for target in resolved)
    return [int(match.group(1)) for match in matches if match]


def _assign(target: ast.AST, assigned: set[str], env: Env) -> None:
    if isinstance(target, ast.Name):
        env[target.id] = set(assigned)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _assign(item, set(), env)


def _assignment_targets(statement: ast.Assign | ast.AnnAssign) -> list[ast.AST]:
    if isinstance(statement, ast.Assign):
        return statement.targets
    return [statement.target]


def _imported_binding(module: str | None, name: str) -> str | None:
    if module in _TRANSACTION_MODULES:
        return _transaction_binding(name)
    return _library_binding(module, name)


def _transaction_binding(name: str) -> str | None:
    if name in _BOUNDARIES:
        return f"<canonical:{name}>"
    if name == "MarkdownCoordinator":
        return "<coordinator-class>"
    return None


def _library_binding(module: str | None, name: str) -> str | None:
    if module == "daily_log_append" and name in _DAILY_APPENDERS:
        return f"<canonical:{name}>"
    if module == "pathlib" and name == "Path":
        return "<path-constructor>"
    return None


def _annotations(arguments: tuple[ast.arg, ...]) -> list[ast.AST]:
    return [argument.annotation for argument in arguments if argument.annotation is not None]


def _definition_expressions(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Decorators, defaults, annotations and type parameters: evaluated where the def runs."""
    arguments = statement.args
    expressions = [
        *statement.decorator_list,
        *arguments.defaults,
        *(item for item in arguments.kw_defaults if item is not None),
        *_annotations((*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)),
    ]
    expressions.extend(_annotations(tuple(filter(None, (arguments.vararg, arguments.kwarg)))))
    if statement.returns is not None:
        expressions.append(statement.returns)
    expressions.extend(getattr(statement, "type_params", ()))
    return expressions


def _class_expressions(statement: ast.ClassDef) -> list[ast.AST]:
    return [
        *statement.decorator_list,
        *statement.bases,
        *(keyword.value for keyword in statement.keywords),
        *getattr(statement, "type_params", ()),
    ]


def _coordinator_annotated(argument: ast.arg, env: Env) -> bool:
    return "<coordinator-class>" in _value(argument.annotation, env)


def _method_offset(arguments: list[ast.arg]) -> int:
    return int(bool(arguments) and arguments[0].arg in {"self", "cls"})


def _positional_binding(function_name: str, argument: ast.arg, position: int, env: Env) -> set[str]:
    if _coordinator_annotated(argument, env):
        return {"<coordinator-instance>"}
    if function_name in _ARCHIVE_BUILD_FUNCTIONS and argument.arg in _BUILD_PARAMETERS:
        return {".bag-building"}
    return {f"<param:{position}>"}


def _keyword_binding(argument: ast.arg, env: Env) -> set[str]:
    if _coordinator_annotated(argument, env):
        return {"<coordinator-instance>"}
    return set()


def _function_env(statement: ast.FunctionDef | ast.AsyncFunctionDef, env: Env) -> Env:
    """The environment a function body starts from: the enclosing one plus its parameters."""
    function_env = _copy_env(env)
    offset = _method_offset(statement.args.args)
    for index, argument in enumerate(statement.args.args):
        function_env[argument.arg] = _positional_binding(statement.name, argument, index - offset, env)
    for argument in statement.args.kwonlyargs:
        function_env[argument.arg] = _keyword_binding(argument, env)
    return function_env


def _loop_header(statement: ast.For | ast.AsyncFor | ast.While) -> ast.AST:
    if isinstance(statement, ast.While):
        return statement.test
    return statement.iter


def _irrefutable(pattern: ast.AST) -> bool:
    return isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None


def _deleted_module_names(tree: ast.Module) -> set[str]:
    return {
        target.id
        for statement in tree.body
        if isinstance(statement, ast.Delete)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }


class _PythonWriteScanner:
    """Flow-sensitive walk that names every call whose target can be covered knowledge.

    Parameters that reach a write are summarized per function, and the walk
    repeats (at most four times) until the summaries stop changing, so a
    call through a helper reports the helper's writes.
    """

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.deleted_names = _deleted_module_names(tree)
        self.findings: list[tuple[int, str, bool, str]] = []
        self.summaries: dict[str, list[tuple[int, str, bool]]] = {}
        self.handlers = {
            ast.ImportFrom: self._import_from,
            ast.Import: self._import,
            ast.Assign: self._assignment,
            ast.AnnAssign: self._assignment,
            ast.FunctionDef: self._function,
            ast.AsyncFunctionDef: self._function,
            ast.ClassDef: self._class,
            ast.If: self._if,
            ast.For: self._loop,
            ast.AsyncFor: self._loop,
            ast.While: self._loop,
            ast.With: self._with,
            ast.AsyncWith: self._with,
            ast.Try: self._try,
            getattr(ast, "TryStar", ast.Try): self._try,
            ast.Match: self._match,
            ast.Delete: self._delete,
        }

    def run(self) -> list[tuple[int, str, bool, str]]:
        initial: Env = {"ROOT": {"<root>"}, "Path": {"<path-constructor>"}}
        for _ in range(4):
            before = repr(self.summaries)
            self.process(self.tree.body, initial)
            if repr(self.summaries) == before:
                break
        return list(dict.fromkeys(self.findings))

    def process(self, statements: list[ast.stmt], inherited: Env, function: str = "") -> Env:
        env = _copy_env(inherited)
        for statement in statements:
            handler = self.handlers.get(type(statement), self._expressions)
            env = handler(statement, env, function)
        return env

    def scan_expression(self, expression: ast.AST | None, env: Env, function: str) -> None:
        if expression is None:
            return
        for call in (item for item in ast.walk(expression) if isinstance(item, ast.Call)):
            self.record(call, env, function)

    def record(self, call: ast.Call, env: Env, function: str) -> None:
        name = _call_name(call)
        resolved, binding_approved, reported_name = self.targets(call, env)
        self._summarize(function, resolved, reported_name, binding_approved)
        archive_operation = _archive_operation(function, name)
        resolved = _archive_targets(resolved, function, archive_operation)
        allowed = _archive_allowed(resolved, function, name)
        if not _reportable(resolved, archive_operation and allowed):
            return
        approved = _finding_approval(function, binding_approved, archive_operation, allowed)
        self.findings.append((call.lineno, reported_name, approved, function or "<module>"))

    def targets(self, call: ast.Call, env: Env) -> tuple[list[str], bool, str]:
        name = _call_name(call)
        bindings = _call_bindings(call, env)
        boundaries = _tagged(bindings, "canonical")
        summarized = self._summarized_targets(call, env, _tagged(bindings, "function"), name)
        if summarized is not None:
            return summarized
        return _direct_targets(call, env, name, next(iter(boundaries), None))

    def _summarized_targets(
        self, call: ast.Call, env: Env, function_names: set[str], name: str
    ) -> tuple[list[str], bool, str] | None:
        summaries = self._call_summaries(function_names)
        if not summaries:
            return None
        result = _summary_argument_values(call, env, summaries)
        return sorted(result), _summaries_approved(summaries), _reported_name(function_names, name)

    def _call_summaries(self, function_names: set[str]) -> list[list[tuple[int, str, bool]]]:
        return [self.summaries[item] for item in function_names if self.summaries.get(item)]

    def _summarize(self, function: str, resolved: list[str], reported_name: str, approved: bool) -> None:
        if not function:
            return
        for index in _parameter_indexes(resolved):
            self._add_summary(function, (index, reported_name, approved))

    def _add_summary(self, function: str, item: tuple[int, str, bool]) -> None:
        summary = self.summaries.setdefault(function, [])
        if item not in summary:
            summary.append(item)

    def _optional_block(self, statements: list[ast.stmt], env: Env, function: str) -> Env:
        if not statements:
            return env
        return self.process(statements, env, function)

    def _import_from(self, statement: ast.ImportFrom, env: Env, function: str) -> Env:
        for item in statement.names:
            binding = _imported_binding(statement.module, item.name)
            if binding is not None:
                env[item.asname or item.name] = {binding}
        return env

    def _import(self, statement: ast.Import, env: Env, function: str) -> Env:
        for item in statement.names:
            if item.name in _TRANSACTION_MODULES:
                env[item.asname or item.name] = {"<canonical-module>"}
        return env

    def _assignment(self, statement: ast.Assign | ast.AnnAssign, env: Env, function: str) -> Env:
        self.scan_expression(statement.value, env, function)
        assigned = _value(statement.value, env)
        for target in _assignment_targets(statement):
            _assign(target, assigned, env)
        return env

    def _function(self, statement: ast.FunctionDef | ast.AsyncFunctionDef, env: Env, function: str) -> Env:
        if statement.name in self.deleted_names:
            env.pop(statement.name, None)
            return env
        for expression in _definition_expressions(statement):
            self.scan_expression(expression, env, function)
        env[statement.name] = {f"<function:{statement.name}>"}
        self.process(statement.body, _function_env(statement, env), statement.name)
        return env

    def _class(self, statement: ast.ClassDef, env: Env, function: str) -> Env:
        for expression in _class_expressions(statement):
            self.scan_expression(expression, env, function)
        env[statement.name] = {f"<function:{statement.name}>"}
        self.process(statement.body, env, function)
        return env

    def _if(self, statement: ast.If, env: Env, function: str) -> Env:
        self.scan_expression(statement.test, env, function)
        body = self.process(statement.body, env, function)
        other = self._optional_block(statement.orelse, env, function)
        return _merge(body, other)

    def _loop(self, statement: ast.For | ast.AsyncFor | ast.While, env: Env, function: str) -> Env:
        self.scan_expression(_loop_header(statement), env, function)
        loop_env = _copy_env(env)
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            _assign(statement.target, set(), loop_env)
        for _ in range(2):
            loop_env = _merge(loop_env, self.process(statement.body, loop_env, function))
        env = _merge(env, loop_env)
        return self._merged_else(statement.orelse, env, function)

    def _merged_else(self, statements: list[ast.stmt], env: Env, function: str) -> Env:
        if not statements:
            return env
        return _merge(env, self.process(statements, env, function))

    def _with(self, statement: ast.With | ast.AsyncWith, env: Env, function: str) -> Env:
        for item in statement.items:
            self._with_item(item, env, function)
        return self.process(statement.body, env, function)

    def _with_item(self, item: ast.withitem, env: Env, function: str) -> None:
        self.scan_expression(item.context_expr, env, function)
        if item.optional_vars:
            _assign(item.optional_vars, _value(item.context_expr, env), env)

    def _try(self, statement: ast.Try, env: Env, function: str) -> Env:
        normal, exceptional = self._try_body(statement, env, function)
        alternatives = [normal, exceptional]
        alternatives.extend(
            self.process(handler.body, exceptional, function) for handler in statement.handlers
        )
        return self._optional_block(statement.finalbody, _merge(*alternatives), function)

    def _try_body(self, statement: ast.Try, env: Env, function: str) -> tuple[Env, Env]:
        """(state after the body and else, merge of every state an exception can leave)."""
        normal = env
        exception_states = [env]
        for body_statement in statement.body:
            normal = self.process([body_statement], normal, function)
            exception_states.append(normal)
        normal = self._optional_block(statement.orelse, normal, function)
        return normal, _merge(*exception_states)

    def _match(self, statement: ast.Match, env: Env, function: str) -> Env:
        self.scan_expression(statement.subject, env, function)
        branches = [self.process(case.body, env, function) for case in statement.cases]
        if any(_irrefutable(case.pattern) for case in statement.cases):
            return _merge(*branches)
        return _merge(env, *branches)

    def _delete(self, statement: ast.Delete, env: Env, function: str) -> Env:
        for target in statement.targets:
            if isinstance(target, ast.Name):
                env.pop(target.id, None)
        return env

    def _expressions(self, statement: ast.stmt, env: Env, function: str) -> Env:
        for expression in ast.iter_child_nodes(statement):
            if isinstance(expression, ast.expr):
                self.scan_expression(expression, env, function)
        return env


def _python_write_calls(source: str) -> list[tuple[int, str, bool, str]]:
    return _PythonWriteScanner(ast.parse(source)).run()


class _CommentScanner:
    """Tracks quotes and escapes across one line, one character at a time."""

    def __init__(self) -> None:
        self.quote = ""
        self.escaped = False

    def plain(self, char: str) -> bool:
        """Advance over one character; True when it is unquoted and unescaped."""
        if self.escaped:
            self.escaped = False
            return False
        if char == "\\":
            self.escaped = True
            return False
        return self._outside_quotes(char)

    def _outside_quotes(self, char: str) -> bool:
        if self.quote:
            self._close_quote(char)
            return False
        if char in {'"', "'"}:
            self.quote = char
            return False
        return True

    def _close_quote(self, char: str) -> None:
        if char == self.quote:
            self.quote = ""


def _strip_comment(line: str, marker: str) -> str:
    scanner = _CommentScanner()
    for index, char in enumerate(line):
        if scanner.plain(char) and line.startswith(marker, index):
            return line[:index]
    return line


def _expr_value(expression: str, variables: dict[str, str]) -> str:
    result = expression
    for name, resolved in sorted(variables.items(), key=lambda item: -len(item[0])):
        result = re.sub(rf"(?<![\w$])\$?{re.escape(name)}\b", resolved, result)
    literals = re.findall(r"['\"]([^'\"]*)['\"]", result)
    unquoted = re.sub(r"['\"][^'\"]*['\"]", "", result)
    pieces = [*re.findall(r"(?:<root>|[A-Za-z]:)?[/\\]?[\w.<>/-]+", unquoted), *literals]
    return "/".join(piece.strip("/\\") for piece in pieces if piece).replace("\\", "/")


_CONTINUATIONS = {".ps1": r"`\s*\n", ".sh": r"\\\s*\n"}


class _StatementJoiner:
    """Joins physical lines into logical statements for one script language."""

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix
        self.statements: list[tuple[int, str]] = []
        self.buffer: list[str] = []
        self.start = 1
        self.balance = 0

    def add(self, line_number: int, raw: str) -> None:
        if not self.buffer:
            self.start = line_number
        self.buffer.append(raw)
        if self._complete(raw):
            self._emit()

    def finish(self) -> list[tuple[int, str]]:
        if self.buffer:
            self.statements.append((self.start, "\n".join(self.buffer)))
        return self.statements

    def _complete(self, raw: str) -> bool:
        stripped = raw.rstrip()
        if self.suffix == ".js":
            return self._javascript_complete(raw, stripped)
        if self.suffix == ".ps1":
            return not stripped.endswith("`")
        return not stripped.endswith("\\")

    def _javascript_complete(self, raw: str, stripped: str) -> bool:
        cleaned = _strip_comment(raw, "//")
        self.balance += sum(cleaned.count(char) for char in "({[")
        self.balance -= sum(cleaned.count(char) for char in ")}]")
        return self.balance <= 0 and stripped.endswith(";")

    def _emit(self) -> None:
        self.statements.append((self.start, _joined_statement(self.suffix, self.buffer)))
        self.buffer = []
        self.balance = 0


def _joined_statement(suffix: str, buffer: list[str]) -> str:
    text = "\n".join(buffer)
    continuation = _CONTINUATIONS.get(suffix)
    if continuation is None:
        return text
    return re.sub(continuation, " ", text)


def _logical_statements(suffix: str, source: str) -> list[tuple[int, str]]:
    joiner = _StatementJoiner(suffix)
    for line_number, raw in enumerate(source.splitlines(), 1):
        joiner.add(line_number, raw)
    return joiner.finish()


_ASSIGNMENT_RE = re.compile(r"(?:const|let|var)?\s*\$?([A-Za-z_]\w*)\s*=\s*(.+?);?$", re.DOTALL)
_JS_WRITE_RE = re.compile(
    r"(?:\w+\.)?(writeFileSync|writeFile|appendFileSync|appendFile|renameSync|rename|unlinkSync|unlink|rmSync|mkdirSync)\s*\((.*)\)",
    re.DOTALL,
)
_POWERSHELL_WRITE_RE = re.compile(
    r"(Set-Content|Add-Content|Out-File|Move-Item|Remove-Item|New-Item)\b(.*)", re.IGNORECASE
)
_POWERSHELL_PATH_FLAGS = ("-Path", "-LiteralPath", "-Destination", "-FilePath")
_REDIRECT_RE = re.compile(r"(?:^|\s)(>>|>)(?![>=])\s*([^\s]+)\s*$")
_SHELL_WRITERS = frozenset({"mv", "rm", "touch", "mkdir"})


def _record_assignment(line: str, variables: dict[str, str]) -> bool:
    """Record `name = expression` into the variables; whether the line was one."""
    assignment = _ASSIGNMENT_RE.match(line)
    if not assignment:
        return False
    variables[assignment.group(1)] = _expr_value(assignment.group(2), variables)
    return True


def _javascript_write(line: str, variables: dict[str, str]) -> tuple[str, str] | None:
    match = _JS_WRITE_RE.search(line)
    if not match:
        return None
    args = [item.strip() for item in match.group(2).split(",")]
    index = 1 if match.group(1) in {"renameSync", "rename"} else 0
    target = _expr_value(args[index], variables) if len(args) > index else ""
    return match.group(1), target


def _powershell_write(line: str, variables: dict[str, str]) -> tuple[str, str] | None:
    match = _POWERSHELL_WRITE_RE.match(line)
    if not match:
        return None
    tokens = shlex.split(match.group(2), posix=False)
    return match.group(1), _expr_value(_powershell_destination(tokens), variables)


def _powershell_destination(tokens: list[str]) -> str:
    destination = tokens[-1] if tokens else ""
    for flag in _POWERSHELL_PATH_FLAGS:
        if flag in tokens and tokens.index(flag) + 1 < len(tokens):
            destination = tokens[tokens.index(flag) + 1]
    return destination


def _shell_write(line: str, variables: dict[str, str]) -> tuple[str, str] | None:
    redirect = _REDIRECT_RE.search(line)
    if redirect:
        return redirect.group(1), _expr_value(redirect.group(2), variables)
    tokens = _shell_tokens(line)
    if tokens and tokens[0] in _SHELL_WRITERS:
        return tokens[0], _expr_value(tokens[-1], variables)
    return None


def _shell_tokens(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


_WRITE_PARSERS = {".js": _javascript_write, ".ps1": _powershell_write}


def _statement_write(suffix: str, line: str, variables: dict[str, str]) -> tuple[str, str] | None:
    """(api, target) of one write statement; None for a blank line, an assignment or no write."""
    if not line:
        return None
    if _record_assignment(line, variables):
        return None
    return _WRITE_PARSERS.get(suffix, _shell_write)(line, variables)


def _non_python_calls(suffix: str, source: str) -> list[tuple[int, str, str]]:
    variables: dict[str, str] = {"root": "<root>"}
    marker = "//" if suffix == ".js" else "#"
    findings: list[tuple[int, str, str]] = []
    for line_number, raw in _logical_statements(suffix, source):
        write = _statement_write(suffix, _strip_comment(raw, marker).strip(), variables)
        if write is not None:
            findings.append((line_number, *write))
    return findings


def scan_source(path: Path, source: str) -> list[WriterFinding]:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return _python_findings(path, source)
    if suffix in {".js", ".ps1", ".sh"}:
        return [
            WriterFinding(path, line, api)
            for line, api, target in _non_python_calls(suffix, source)
            if _covered(target)
        ]
    return []


def _python_findings(path: Path, source: str) -> list[WriterFinding]:
    findings = [
        WriterFinding(path, line, api, approved, function)
        for line, api, approved, function in _python_write_calls(source)
    ]
    if path.name == "markdown_transaction.py":
        findings.extend(_transaction_apply_findings(path, ast.parse(source)))
    return findings


def _transaction_apply_findings(path: Path, tree: ast.Module) -> list[WriterFinding]:
    """The transaction's own apply step is the one approved direct writer."""
    return [
        WriterFinding(path, call.lineno, _call_name(call), True)
        for function in _apply_functions(tree)
        for call in _calls_in(function)
        if _call_name(call) in _APPLY_WRITES
    ]


def _apply_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in _APPLY_FUNCTIONS
    ]


def _calls_in(node: ast.AST) -> list[ast.Call]:
    return [item for item in ast.walk(node) if isinstance(item, ast.Call)]


def _source_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory in SEARCH_DIRS:
        paths.extend(_executables_under(root / directory))
    paths.extend(path for path in root.iterdir() if _root_installer(path))
    return sorted(set(paths))


def _executables_under(base: Path) -> list[Path]:
    if not base.exists():
        return []
    return [path for path in base.rglob("*") if _executable(path)]


def _executable(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() in EXECUTABLE_SUFFIXES


def _root_installer(path: Path) -> bool:
    if not _executable(path):
        return False
    name = path.name.casefold()
    return "install" in name or name.startswith("setup")


def discover_repository_writers(root: Path) -> list[WriterFinding]:
    root = Path(root).resolve()
    findings: list[WriterFinding] = []
    for path in _source_paths(root):
        try:
            source = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        findings.extend(scan_source(path.relative_to(root), source))
    return findings


def discover_repository_entrypoints(
    root: Path, *, files: set[str]
) -> set[str]:
    """Return Task writer functions that directly invoke a proven boundary."""
    root = Path(root).resolve()
    result: set[str] = set()
    for path in _source_paths(root):
        if path.name in files and path.suffix.casefold() == ".py":
            result.update(_boundary_entrypoints(path, root))
    return result


def _boundary_entrypoints(path: Path, root: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="strict"))
    canonical, _, aliases, canonical_modules = _python_bindings(tree)
    relative = path.relative_to(root).as_posix()
    return {
        f"{relative}:{function.name}"
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _invokes_boundary(function, canonical, aliases, canonical_modules)
    }


def _invokes_boundary(
    function: ast.AST, canonical: dict[str, str], aliases: dict[str, str], canonical_modules: set[str]
) -> bool:
    return any(
        _boundary_call(call, canonical, aliases, canonical_modules) for call in _calls_in(function)
    )


def _boundary_call(
    call: ast.Call, canonical: dict[str, str], aliases: dict[str, str], canonical_modules: set[str]
) -> bool:
    name = _call_name(call)
    if name in canonical or aliases.get(name, name) in canonical:
        return True
    return _module_boundary(call.func, canonical_modules)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).parent.parent)
    args = parser.parse_args()
    findings = discover_repository_writers(args.root)
    for finding in findings:
        print(finding)
    return 1 if any(not finding.approved for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
