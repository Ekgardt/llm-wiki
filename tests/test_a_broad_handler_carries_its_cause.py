"""A broad exception handler that answers with a verdict carries the exception.

`inspect_installed_vault` turned every exception into `conflict` /
`reliability_v3_record_invalid`, the repair command into `repair_backend_error`,
and doctor's adoption probe into `unknown`: a busy database on one machine was
reported as corrupt records, and the installer said capture was disabled. Each
handler caught everything, dropped what it caught, and answered with a fixed code.

This walks every module in `scripts/` and refuses a handler for `Exception`,
`BaseException` or everything that never reads what it caught and answers with
a verdict: a returned failure-vocabulary string, a returned call to a report,
result, error or failure builder, or a raise `from None`. A handler that returns
a fallback value is not a verdict and is not refused; a raise inside a handler
keeps the caught exception as its context.
See docs/research/2026-09-28-a-check-names-its-cause.md.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
BROAD = frozenset({"Exception", "BaseException"})
FAILURE_WORDS = re.compile(r"error|fail|invalid|conflict|unsafe|unreadable|unknown|refused")
VERDICT_BUILDER = re.compile(r"(report|result|error|failure)$")


def _caught(handler: ast.ExceptHandler) -> list[ast.expr]:
    if isinstance(handler.type, ast.Tuple):
        return list(handler.type.elts)
    return [handler.type] if handler.type is not None else []


def _is_broad(handler: ast.ExceptHandler) -> bool:
    caught = _caught(handler)
    return not caught or any(isinstance(item, ast.Name) and item.id in BROAD for item in caught)


def _reads_what_it_caught(handler: ast.ExceptHandler) -> bool:
    nodes = (node for statement in handler.body for node in ast.walk(statement))
    return any(isinstance(node, ast.Name) and node.id == handler.name for node in nodes)


def _callee(call: ast.Call) -> str:
    function = call.func
    return function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")


def _is_verdict(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return bool(FAILURE_WORDS.search(value.value))
    return isinstance(value, ast.Call) and bool(VERDICT_BUILDER.search(_callee(value)))


def _answers_with_verdict(node: ast.AST) -> bool:
    if isinstance(node, ast.Return) and node.value is not None:
        return _is_verdict(node.value)
    return isinstance(node, ast.Raise) and isinstance(node.cause, ast.Constant)


def _answers_in_body(handler: ast.ExceptHandler) -> bool:
    nodes = (node for part in handler.body for node in ast.walk(part))
    return any(_answers_with_verdict(node) for node in nodes)


def _drops_its_cause(handler: ast.ExceptHandler) -> bool:
    if not _is_broad(handler):
        return False
    return not _reads_what_it_caught(handler) and _answers_in_body(handler)


def _dropping_handlers(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    return [f"{path.name}:{handler.lineno}" for handler in handlers if _drops_its_cause(handler)]


def test_no_broad_handler_answers_with_a_verdict_it_cannot_explain() -> None:
    found = [site for path in sorted(SCRIPTS.glob("*.py")) for site in _dropping_handlers(path)]
    assert found == []


def test_the_guard_refuses_the_shape_that_hid_the_cause(tmp_path: Path) -> None:
    """The three handlers this was written for, as they stood, are refused."""
    module = tmp_path / "shapes.py"
    module.write_text(
        "def inspect():\n"
        "    try:\n        return read()\n"
        "    except Exception:\n"
        "        return _report(state='conflict', blockers=['reliability_v3_record_invalid'])\n"
        "def repair():\n"
        "    try:\n        return run()\n"
        "    except Exception:\n        return _backend_error('check')\n"
        "def adoption():\n"
        "    try:\n        return probe()\n"
        "    except Exception:\n        return 'unknown'\n"
        "def fallback():\n"
        "    try:\n        return probe()\n"
        "    except Exception:\n        return None\n",
        encoding="utf-8",
    )
    assert _dropping_handlers(module) == ["shapes.py:4", "shapes.py:9", "shapes.py:14"]
