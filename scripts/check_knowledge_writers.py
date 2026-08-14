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


def _python_bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], set[str], dict[str, str], set[str]]:
    canonical: dict[str, str] = {}
    path_aliases = {"Path"}
    aliases: dict[str, str] = {}
    canonical_modules: set[str] = set()
    for statement in ast.walk(tree):
        if isinstance(statement, ast.ImportFrom):
            if statement.module in {"markdown_transaction", "scripts.markdown_transaction"}:
                for item in statement.names:
                    if item.name in _BOUNDARIES:
                        canonical[item.asname or item.name] = item.name
            if statement.module == "daily_log_append":
                for item in statement.names:
                    if item.name in {"locked_append", "locked_append_once", "append_daily"}:
                        canonical[item.asname or item.name] = item.name
            if statement.module == "pathlib":
                for item in statement.names:
                    if item.name == "Path":
                        path_aliases.add(item.asname or item.name)
        elif isinstance(statement, ast.Import):
            for item in statement.names:
                if item.name in {"markdown_transaction", "scripts.markdown_transaction"}:
                    canonical_modules.add(item.asname or item.name)
        elif isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Name):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = statement.value.id
                    if statement.value.id in canonical:
                        canonical[target.id] = canonical[statement.value.id]
        elif (
            isinstance(statement, ast.Assign)
            and isinstance(statement.value, ast.Attribute)
            and isinstance(statement.value.value, ast.Name)
            and statement.value.value.id in canonical_modules
            and statement.value.attr in _BOUNDARIES
        ):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = statement.value.attr
                    canonical[target.id] = statement.value.attr
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            canonical.pop(statement.name, None)
    return canonical, path_aliases, aliases, canonical_modules


def _python_write_calls(source: str) -> list[tuple[int, str, bool, str]]:
    tree = ast.parse(source)
    deleted_names = {
        target.id
        for statement in tree.body
        if isinstance(statement, ast.Delete)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    findings: list[tuple[int, str, bool, str]] = []
    summaries: dict[str, list[tuple[int, str, bool]]] = {}
    Env = dict[str, set[str]]

    def value(node: ast.AST | None, env: Env) -> set[str]:
        if node is None:
            return set()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value.replace("\\", "/")}
        if isinstance(node, ast.Name):
            return set(env.get(node.id, set()))
        if isinstance(node, ast.FormattedValue):
            return {"<value>"}
        if isinstance(node, ast.JoinedStr):
            result = {""}
            for item in node.values:
                parts = value(item, env) or {""}
                result = {left + right for left in result for right in parts}
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            left, right = value(node.left, env), value(node.right, env)
            if not left:
                return right
            if not right:
                return left
            separator = "/" if isinstance(node.op, ast.Div) else ""
            return {f"{a}{separator}{b}" for a in left for b in right}
        if isinstance(node, ast.Call):
            constructors = value(node.func, env)
            if "<path-constructor>" in constructors and node.args:
                return value(node.args[0], env)
            if "<coordinator-class>" in constructors:
                return {"<coordinator-instance>"}
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "absolute", "resolve", "with_name", "with_suffix",
            }:
                return value(node.func.value, env)
        if isinstance(node, ast.Attribute):
            base = value(node.value, env)
            if "<canonical-module>" in base and node.attr in _BOUNDARIES:
                return {f"<canonical:{node.attr}>"}
            return {f"{item}/{node.attr}" for item in base} or set(env.get(node.attr, set()))
        return set()

    def call_bindings(call: ast.Call, env: Env) -> set[str]:
        if isinstance(call.func, ast.Name):
            return set(env.get(call.func.id, set())) or {f"<function:{call.func.id}>"}
        if isinstance(call.func, ast.Attribute):
            modules = value(call.func.value, env)
            if "<canonical-module>" in modules and call.func.attr in _BOUNDARIES:
                return {f"<canonical:{call.func.attr}>"}
            if "<coordinator-instance>" in modules and call.func.attr == "ensure_target_parent":
                return {"<canonical:ensure_target_parent>"}
            return {f"<function:{call.func.attr}>"}
        return set()

    def targets(call: ast.Call, env: Env) -> tuple[list[str], bool, str]:
        name = _call_name(call)
        bindings = call_bindings(call, env)
        boundaries = {
            item.removeprefix("<canonical:").removesuffix(">")
            for item in bindings
            if item.startswith("<canonical:")
        }
        function_names = {
            item.removeprefix("<function:").removesuffix(">")
            for item in bindings
            if item.startswith("<function:")
        }
        result: set[str] = set()
        approved = True
        summarized = False
        for function_name in function_names:
            summary = summaries.get(function_name)
            if not summary:
                continue
            summarized = True
            approved = approved and all(item[2] for item in summary)
            for index, _, _ in summary:
                if len(call.args) > index:
                    result.update(value(call.args[index], env))
        if summarized:
            reported = next(iter(function_names)) if len(function_names) == 1 else name
            return sorted(result), approved, reported
        boundary = next(iter(boundaries), None)
        apparent_boundary = boundary or (name if name in _BOUNDARIES else None)
        if apparent_boundary:
            if apparent_boundary == "ensure_target_parent":
                result = value(call.args[0], env) if call.args else set()
            elif apparent_boundary == "mutate_knowledge" and len(call.args) > 1 and isinstance(call.args[1], ast.Dict):
                result = {item for key in call.args[1].keys for item in value(key, env)}
            elif apparent_boundary == "append_knowledge" and len(call.args) == 2:
                result = value(call.args[0], env)
            else:
                index = 1 if apparent_boundary == "append_knowledge" else 0
                result = value(call.args[index], env) if len(call.args) > index else set()
            return sorted(result), boundary is not None, name
        if name == "open":
            if isinstance(call.func, ast.Attribute):
                modes = value(call.args[0], env) if call.args else {"r"}
                result = value(call.func.value, env) if any(any(flag in mode for flag in "wax+") for mode in modes) else set()
                return sorted(result), False, name
            modes = value(call.args[1], env) if len(call.args) > 1 else {"r"}
            result = value(call.args[0], env) if call.args and any(any(flag in mode for flag in "wax+") for mode in modes) else set()
            return sorted(result), False, name
        if name in _PATH_METHODS:
            result = value(call.func.value, env) if isinstance(call.func, ast.Attribute) else set()
            return sorted(result), False, name
        if name in _RENAME_METHODS:
            if (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "MarkdownChange"
            ):
                return [], False, name
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id in {"os", "shutil"}:
                result = {value_ for item in call.args[:2] for value_ in value(item, env)}
                return sorted(result), False, name
            if isinstance(call.func, ast.Attribute):
                result = value(call.func.value, env)
                if call.args:
                    result.update(value(call.args[0], env))
                return sorted(result), False, name
        if name == "atomic_write" and call.args:
            return sorted(value(call.args[0], env)), False, name
        return [], False, name

    def merge(*environments: Env) -> Env:
        return {
            name: set().union(*(env.get(name, set()) for env in environments))
            for name in set().union(*(set(env) for env in environments))
        }

    def scan_expression(expression: ast.AST | None, env: Env, function: str) -> None:
        if expression is None:
            return
        for call in (item for item in ast.walk(expression) if isinstance(item, ast.Call)):
            record(call, env, function)

    def record(call: ast.Call, env: Env, function: str) -> None:
        name = _call_name(call)
        resolved, binding_approved, reported_name = targets(call, env)
        for target in resolved:
            match = re.fullmatch(r"<param:(\d+)>", target)
            if match and function:
                item = (int(match.group(1)), reported_name, binding_approved)
                if item not in summaries.setdefault(function, []):
                    summaries[function].append(item)
        archive_operation = function in _ARCHIVE_BUILD_FUNCTIONS and name in {
            "write_bytes", "unlink", "replace", "rename", "move", "mkdir"
        }
        if archive_operation and not resolved and function in {"_recover_hidden_builds", "_publish_build"}:
            resolved = [".bag-building"]
        hidden = any(".bag" in item or ".building" in item for item in resolved)
        archive_allowed = (hidden and name in {"write_bytes", "unlink", "mkdir"}) or (
            function in {"_recover_hidden_builds", "_publish_build"}
            and name in {"replace", "rename", "move"}
        )
        if not resolved or not (any(_covered(item) for item in resolved) or archive_operation and archive_allowed):
            return
        approved = binding_approved
        if function in {"_apply_operation", "_apply_windows_operation"}:
            approved = True
        if archive_operation:
            approved = archive_allowed
        findings.append((call.lineno, reported_name, approved, function or "<module>"))

    def assign(target: ast.AST, assigned: set[str], env: Env) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = set(assigned)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                assign(item, set(), env)

    def process(statements: list[ast.stmt], inherited: Env, function: str = "") -> Env:
        env = {name: set(items) for name, items in inherited.items()}
        for statement in statements:
            if isinstance(statement, ast.ImportFrom):
                for item in statement.names:
                    local = item.asname or item.name
                    if statement.module in {"markdown_transaction", "scripts.markdown_transaction"} and item.name in _BOUNDARIES:
                        env[local] = {f"<canonical:{item.name}>"}
                    elif statement.module in {"markdown_transaction", "scripts.markdown_transaction"} and item.name == "MarkdownCoordinator":
                        env[local] = {"<coordinator-class>"}
                    elif statement.module == "daily_log_append" and item.name in {"locked_append", "locked_append_once", "append_daily"}:
                        env[local] = {f"<canonical:{item.name}>"}
                    elif statement.module == "pathlib" and item.name == "Path":
                        env[local] = {"<path-constructor>"}
                continue
            if isinstance(statement, ast.Import):
                for item in statement.names:
                    if item.name in {"markdown_transaction", "scripts.markdown_transaction"}:
                        env[item.asname or item.name] = {"<canonical-module>"}
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                scan_expression(statement.value, env, function)
                assigned = value(statement.value, env)
                assigned_targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in assigned_targets:
                    assign(target, assigned, env)
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.name in deleted_names:
                    env.pop(statement.name, None)
                    continue
                definition_expressions = [
                    *statement.decorator_list,
                    *statement.args.defaults,
                    *(item for item in statement.args.kw_defaults if item is not None),
                    *(argument.annotation for argument in (
                        *statement.args.posonlyargs,
                        *statement.args.args,
                        *statement.args.kwonlyargs,
                    ) if argument.annotation is not None),
                ]
                if statement.args.vararg and statement.args.vararg.annotation:
                    definition_expressions.append(statement.args.vararg.annotation)
                if statement.args.kwarg and statement.args.kwarg.annotation:
                    definition_expressions.append(statement.args.kwarg.annotation)
                if statement.returns is not None:
                    definition_expressions.append(statement.returns)
                definition_expressions.extend(getattr(statement, "type_params", ()))
                for expression in definition_expressions:
                    scan_expression(expression, env, function)
                env[statement.name] = {f"<function:{statement.name}>"}
                function_env = {name: set(items) for name, items in env.items()}
                method_offset = int(bool(statement.args.args) and statement.args.args[0].arg in {"self", "cls"})
                for index, argument in enumerate(statement.args.args):
                    marker = f"<param:{index - method_offset}>"
                    if "<coordinator-class>" in value(argument.annotation, env):
                        function_env[argument.arg] = {"<coordinator-instance>"}
                    else:
                        function_env[argument.arg] = {
                            ".bag-building" if statement.name in _ARCHIVE_BUILD_FUNCTIONS and argument.arg in {"build", "publish_build", "hidden_build"} else marker
                        }
                for argument in statement.args.kwonlyargs:
                    function_env[argument.arg] = (
                        {"<coordinator-instance>"}
                        if "<coordinator-class>" in value(argument.annotation, env)
                        else set()
                    )
                process(statement.body, function_env, statement.name)
                continue
            if isinstance(statement, ast.ClassDef):
                definition_expressions = [
                    *statement.decorator_list,
                    *statement.bases,
                    *(keyword.value for keyword in statement.keywords),
                    *getattr(statement, "type_params", ()),
                ]
                for expression in definition_expressions:
                    scan_expression(expression, env, function)
                env[statement.name] = {f"<function:{statement.name}>"}
                process(statement.body, env, function)
                continue
            if isinstance(statement, ast.If):
                scan_expression(statement.test, env, function)
                body = process(statement.body, env, function)
                other = process(statement.orelse, env, function) if statement.orelse else env
                env = merge(body, other)
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                scan_expression(statement.iter if isinstance(statement, (ast.For, ast.AsyncFor)) else statement.test, env, function)
                loop_env = {name: set(items) for name, items in env.items()}
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    assign(statement.target, set(), loop_env)
                for _ in range(2):
                    loop_env = merge(loop_env, process(statement.body, loop_env, function))
                env = merge(env, loop_env)
                if statement.orelse:
                    env = merge(env, process(statement.orelse, env, function))
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    scan_expression(item.context_expr, env, function)
                    if item.optional_vars:
                        assign(item.optional_vars, value(item.context_expr, env), env)
                env = process(statement.body, env, function)
                continue
            if isinstance(statement, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                normal = env
                exception_states = [env]
                for body_statement in statement.body:
                    normal = process([body_statement], normal, function)
                    exception_states.append(normal)
                if statement.orelse:
                    normal = process(statement.orelse, normal, function)
                exceptional = merge(*exception_states)
                alternatives = [normal, exceptional]
                alternatives.extend(process(handler.body, exceptional, function) for handler in statement.handlers)
                env = merge(*alternatives)
                if statement.finalbody:
                    env = process(statement.finalbody, env, function)
                continue
            if isinstance(statement, ast.Match):
                scan_expression(statement.subject, env, function)
                branches = [process(case.body, env, function) for case in statement.cases]
                exhaustive = any(isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None and case.pattern.name is None for case in statement.cases)
                env = merge(*(branches if exhaustive else [env, *branches]))
                continue
            if isinstance(statement, ast.Delete):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        env.pop(target.id, None)
                continue
            for expression in ast.iter_child_nodes(statement):
                if isinstance(expression, ast.expr):
                    scan_expression(expression, env, function)
        return env

    initial: Env = {"ROOT": {"<root>"}, "Path": {"<path-constructor>"}}
    for _ in range(4):
        before = repr(summaries)
        process(tree.body, initial)
        if repr(summaries) == before:
            break
    return list(dict.fromkeys(findings))


def _strip_comment(line: str, marker: str) -> str:
    quote = ""
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif line.startswith(marker, index):
            return line[:index]
        index += 1
    return line


def _expr_value(expression: str, variables: dict[str, str]) -> str:
    result = expression
    for name, resolved in sorted(variables.items(), key=lambda item: -len(item[0])):
        result = re.sub(rf"(?<![\w$])\$?{re.escape(name)}\b", resolved, result)
    literals = re.findall(r"['\"]([^'\"]*)['\"]", result)
    unquoted = re.sub(r"['\"][^'\"]*['\"]", "", result)
    pieces = [*re.findall(r"(?:<root>|[A-Za-z]:)?[/\\]?[\w.<>/-]+", unquoted), *literals]
    return "/".join(piece.strip("/\\") for piece in pieces if piece).replace("\\", "/")


def _logical_statements(suffix: str, source: str) -> list[tuple[int, str]]:
    statements: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 1
    balance = 0
    for line_number, raw in enumerate(source.splitlines(), 1):
        if not buffer:
            start = line_number
        buffer.append(raw)
        stripped = raw.rstrip()
        if suffix == ".js":
            cleaned = _strip_comment(raw, "//")
            balance += sum(cleaned.count(char) for char in "({[")
            balance -= sum(cleaned.count(char) for char in ")}]" )
            complete = balance <= 0 and stripped.endswith(";")
        elif suffix == ".ps1":
            complete = not stripped.endswith("`")
        else:
            complete = not stripped.endswith("\\")
        if complete:
            text = "\n".join(buffer)
            if suffix == ".ps1":
                text = re.sub(r"`\s*\n", " ", text)
            elif suffix == ".sh":
                text = re.sub(r"\\\s*\n", " ", text)
            statements.append((start, text))
            buffer = []
            balance = 0
    if buffer:
        statements.append((start, "\n".join(buffer)))
    return statements


def _non_python_calls(suffix: str, source: str) -> list[tuple[int, str, str]]:
    variables: dict[str, str] = {"root": "<root>"}
    findings: list[tuple[int, str, str]] = []
    for line_number, raw in _logical_statements(suffix, source):
        marker = "//" if suffix == ".js" else "#"
        line = _strip_comment(raw, marker).strip()
        if not line:
            continue
        assignment = re.match(
            r"(?:const|let|var)?\s*\$?([A-Za-z_]\w*)\s*=\s*(.+?);?$",
            line,
            re.DOTALL,
        )
        if assignment:
            variables[assignment.group(1)] = _expr_value(assignment.group(2), variables)
            continue
        if suffix == ".js":
            match = re.search(r"(?:\w+\.)?(writeFileSync|writeFile|appendFileSync|appendFile|renameSync|rename|unlinkSync|unlink|rmSync|mkdirSync)\s*\((.*)\)", line, re.DOTALL)
            if not match:
                continue
            args = [item.strip() for item in match.group(2).split(",")]
            index = 1 if match.group(1) in {"renameSync", "rename"} else 0
            target = _expr_value(args[index], variables) if len(args) > index else ""
            findings.append((line_number, match.group(1), target))
            continue
        if suffix == ".ps1":
            match = re.match(r"(Set-Content|Add-Content|Out-File|Move-Item|Remove-Item|New-Item)\b(.*)", line, re.IGNORECASE)
            if not match:
                continue
            tokens = shlex.split(match.group(2), posix=False)
            destination = tokens[-1] if tokens else ""
            for flag in ("-Path", "-LiteralPath", "-Destination", "-FilePath"):
                if flag in tokens and tokens.index(flag) + 1 < len(tokens):
                    destination = tokens[tokens.index(flag) + 1]
            findings.append((line_number, match.group(1), _expr_value(destination, variables)))
            continue
        redirect = re.search(r"(?:^|\s)(>>|>)(?![>=])\s*([^\s]+)\s*$", line)
        if redirect:
            findings.append((line_number, redirect.group(1), _expr_value(redirect.group(2), variables)))
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()
        if tokens and tokens[0] in {"mv", "rm", "touch", "mkdir"}:
            findings.append((line_number, tokens[0], _expr_value(tokens[-1], variables)))
    return findings


def scan_source(path: Path, source: str) -> list[WriterFinding]:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        findings = [
            WriterFinding(path, line, api, approved, function)
            for line, api, approved, function in _python_write_calls(source)
        ]
        if path.name == "markdown_transaction.py":
            tree = ast.parse(source)
            for function in (
                node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name in {"_apply_operation", "_apply_windows_operation"}
            ):
                for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                    name = _call_name(call)
                    if name in {
                        "unlink", "replace", "durable_publish_file",
                        "_delete_windows_handle",
                    }:
                        findings.append(WriterFinding(path, call.lineno, name, True))
        return findings
    if suffix in {".js", ".ps1", ".sh"}:
        return [
            WriterFinding(path, line, api)
            for line, api, target in _non_python_calls(suffix, source)
            if _covered(target)
        ]
    return []


def _source_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory in SEARCH_DIRS:
        base = root / directory
        if base.exists():
            paths.extend(path for path in base.rglob("*") if path.is_file() and path.suffix.casefold() in EXECUTABLE_SUFFIXES)
    paths.extend(
        path for path in root.iterdir()
        if path.is_file() and path.suffix.casefold() in EXECUTABLE_SUFFIXES
        and ("install" in path.name.casefold() or path.name.casefold().startswith("setup"))
    )
    return sorted(set(paths))


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
        if path.name not in files or path.suffix.casefold() != ".py":
            continue
        source = path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(source)
        canonical, _, aliases, canonical_modules = _python_bindings(tree)
        for function in (
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                name = _call_name(call)
                resolved = aliases.get(name, name)
                module_call = (
                    isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in canonical_modules
                    and name in _BOUNDARIES
                )
                if name in canonical or resolved in canonical or module_call:
                    result.add(f"{path.relative_to(root).as_posix()}:{function.name}")
                    break
    return result


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
