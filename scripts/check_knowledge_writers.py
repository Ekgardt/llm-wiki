"""Find direct writes that can target authoritative knowledge files."""

from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path

EXECUTABLE_SUFFIXES = {".py", ".js", ".ps1", ".sh"}
SEARCH_DIRS = ("scripts", "integrations")
_COVERED_RE = re.compile(
    r"(?:^|/)knowledge/(?:daily|notes|projects|inbox)(?:/|$)|"
    r"(?:^|/)knowledge/(?:index|log)\.md$",
    re.IGNORECASE,
)
PYTHON_WRITE_METHODS = {
    "write_text", "write_bytes", "touch", "unlink", "replace", "rename",
    "move", "atomic_write",
}
NON_PYTHON_WRITE_RE = re.compile(
    r"(?:writeFile|appendFile|rename|unlink|rmSync|mv\s|rm\s|Remove-Item|Move-Item|"
    r"Set-Content|Add-Content|Out-File|(?:^|\s)(?:>>|>)(?!=))",
    re.MULTILINE,
)
NON_PYTHON_COVERED_RE = re.compile(
    r"knowledge[/\\](?:daily|notes|projects|inbox)|knowledge[/\\](?:index|log)\.md",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WriterFinding:
    path: Path
    line: int
    api: str
    approved: bool = False

    def __str__(self) -> str:
        status = "approved" if self.approved else "DIRECT COVERED WRITE"
        return f"{self.path.as_posix()}:{self.line}: {status}: {self.api}"


def _call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _python_write_calls(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    deleted_names = {
        target.id
        for statement in tree.body
        if isinstance(statement, ast.Delete)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    globals_: dict[str, str] = {"ROOT": "<root>"}
    returns: dict[str, str] = {}

    def value(node: ast.AST | None, values: dict[str, str]) -> str:
        if node is None:
            return ""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.replace("\\", "/")
        if isinstance(node, ast.Name):
            return values.get(node.id, "")
        if isinstance(node, ast.JoinedStr):
            return "".join(value(item, values) for item in node.values)
        if isinstance(node, ast.FormattedValue):
            return "<value>"
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            left, right = value(node.left, values), value(node.right, values)
            return f"{left}/{right}" if left and right else left or right
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in returns:
                return returns[name]
            if isinstance(node.func, ast.Name) and node.func.id == "Path" and node.args:
                return value(node.args[0], values)
            if isinstance(node.func, ast.Attribute):
                return value(node.func.value, values)
        if isinstance(node, ast.Attribute):
            return values.get(node.attr, "")
        return ""

    findings: list[tuple[int, str]] = []

    def inspect_call(node: ast.Call, values: dict[str, str]) -> None:
        name = _call_name(node)
        if name in {"mutate_knowledge", "append_knowledge"}:
            return
        target = ""
        if name == "open":
            target = value(node.args[0], values) if node.args else ""
            mode = "r"
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = str(keyword.value.value)
            if not any(flag in mode for flag in "wax+"):
                return
        elif name in PYTHON_WRITE_METHODS:
            if name in {"atomic_write", "locked_append", "locked_append_once", "move"}:
                target = value(node.args[0], values) if node.args else ""
            elif isinstance(node.func, ast.Attribute):
                target = value(node.func.value, values)
        else:
            return
        if _COVERED_RE.search(target.replace("<root>/", "")):
            findings.append((node.lineno, name))

    def process_block(statements: list[ast.stmt], inherited: dict[str, str]) -> None:
        values = dict(inherited)
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                assigned = value(statement.value, values)
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name) and assigned:
                        values[target.id] = assigned
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.name in deleted_names:
                    continue
                process_block(statement.body, values)
                continue
            for node in ast.walk(statement):
                if isinstance(node, ast.Call):
                    inspect_call(node, values)

    # Establish module constants before scanning function-local scopes.
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            assigned = value(statement.value, globals_)
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name) and assigned:
                    globals_[target.id] = assigned
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef):
            for child in ast.walk(statement):
                if isinstance(child, ast.Return):
                    returned = value(child.value, globals_)
                    if _COVERED_RE.search(returned.replace("<root>/", "")):
                        returns[statement.name] = returned
                        break
    process_block(tree.body, globals_)
    return findings


def scan_source(path: Path, source: str) -> list[WriterFinding]:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        calls = _python_write_calls(source)
        result = []
        for line, api in calls:
            approved = (
                path.name == "markdown_transaction.py"
                and api in {"write_bytes", "replace", "unlink"}
            ) or (
                path.name == "archive_daily.py"
                and api in {"write_bytes", "replace", "rename", "move", "unlink"}
            )
            result.append(WriterFinding(path, line, api, approved))
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
                        "unlink", "replace", "_rename_windows_handle",
                        "_delete_windows_handle",
                    }:
                        result.append(WriterFinding(path, call.lineno, name, True))
        return result
    if suffix in {".js", ".ps1", ".sh"} and NON_PYTHON_COVERED_RE.search(source):
        lines = source.splitlines()
        findings = []
        for match in NON_PYTHON_WRITE_RE.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            context = "\n".join(lines[max(0, line - 4):line + 3])
            if NON_PYTHON_COVERED_RE.search(context):
                findings.append(WriterFinding(path, line, match.group(0)))
        return findings
    return []


def _source_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory in SEARCH_DIRS:
        base = root / directory
        if base.exists():
            paths.extend(
                path for path in base.rglob("*")
                if path.is_file() and path.suffix.casefold() in EXECUTABLE_SUFFIXES
            )
    paths.extend(
        path for path in root.iterdir()
        if path.is_file()
        and path.suffix.casefold() in EXECUTABLE_SUFFIXES
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
        relative = path.relative_to(root)
        findings.extend(scan_source(relative, source))
    return findings


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
