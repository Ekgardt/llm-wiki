"""A broad exception handler that says something failed carries the exception.

`inspect_installed_vault` turned every exception into `conflict` /
`reliability_v3_record_invalid`, the repair command into `repair_backend_error`,
and doctor's adoption probe into `unknown`: a busy database on one machine was
reported as corrupt records, and the installer said capture was disabled. Each
handler caught everything, dropped what it caught, and answered with a fixed code.

The same shape stood in the answer paths: the reranker answered `reranker_error`,
the graph backend `graph_error`, a navigation callback `NavigationStatus.ERROR`,
the code graph fell back to regex parsing, each with the exception thrown away.

This walks every module in `scripts/` and refuses a handler for `Exception`,
`BaseException` or everything that never reads what it caught (by name, or through
`traceback.format_exc`, `sys.exc_info` or `logging.exception`) and either

- names a failure anywhere in its body: a failure-vocabulary string (returned,
  assigned, passed or packed in a tuple), a failure-vocabulary attribute such as
  `Status.ERROR` or `self.x_failed`, a call to a report, result, error or failure
  builder, or a raise `from None`; or
- falls back in a module that builds an answer from what it computes
  (`ANSWER_MODULES`): returns a value or assigns one instead of the one it could
  not compute. There a fallback is a worse answer that looks like a good one, and
  only its cause tells them apart.

A handler that only passes or continues is cleanup (a failed close after the
answer) and is not refused; a raise inside a handler keeps the caught exception as
its context. Elsewhere a silent value fallback is not yet refused: each needs its
own look at whether its caller or the log already says why.
See docs/research/2026-09-28-a-check-names-its-cause.md.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
BROAD = frozenset({"Exception", "BaseException"})
FAILURE_WORDS = re.compile(
    r"error|fail|invalid|conflict|unsafe|unreadable|unknown|refused", re.IGNORECASE
)
VERDICT_BUILDER = re.compile(r"(report|result|error|failure)$")
# Calls that read the exception being handled without naming it.
ACTIVE_EXCEPTION_READERS = frozenset({"format_exc", "print_exc", "exc_info", "exception"})
# The modules whose fallbacks become the ranked or code answer an agent reads.
ANSWER_MODULES = frozenset({"retrieval.py", "reranker.py", "code_graph.py", "code_navigation.py"})


def _caught(handler: ast.ExceptHandler) -> list[ast.expr]:
    if isinstance(handler.type, ast.Tuple):
        return list(handler.type.elts)
    return [handler.type] if handler.type is not None else []


def _is_broad(handler: ast.ExceptHandler) -> bool:
    caught = _caught(handler)
    return not caught or any(isinstance(item, ast.Name) and item.id in BROAD for item in caught)


def _body_nodes(handler: ast.ExceptHandler) -> list[ast.AST]:
    return [node for statement in handler.body for node in ast.walk(statement)]


def _callee(call: ast.Call) -> str:
    function = call.func
    return function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")


def _reads(node: ast.AST, name: str | None) -> bool:
    if isinstance(node, ast.Name):
        return node.id == name
    return isinstance(node, ast.Call) and _callee(node) in ACTIVE_EXCEPTION_READERS


def _reads_what_it_caught(handler: ast.ExceptHandler) -> bool:
    return any(_reads(node, handler.name) for node in _body_nodes(handler))


def _failure_word(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return node.attr if isinstance(node, ast.Attribute) else ""


def _names_a_failure(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return bool(VERDICT_BUILDER.search(_callee(node)))
    if isinstance(node, ast.Raise):
        return isinstance(node.cause, ast.Constant)
    return bool(FAILURE_WORDS.search(_failure_word(node)))


def _falls_back(node: ast.AST) -> bool:
    if isinstance(node, ast.Return):
        return node.value is not None
    return isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))


def _says_it_failed(handler: ast.ExceptHandler, module: str) -> bool:
    nodes = _body_nodes(handler)
    if any(_names_a_failure(node) for node in nodes):
        return True
    return module in ANSWER_MODULES and any(_falls_back(node) for node in nodes)


def _keeps_it_as_context(handler: ast.ExceptHandler) -> bool:
    """A raise that is not `from None` carries the caught exception onward."""
    raises = (node for node in _body_nodes(handler) if isinstance(node, ast.Raise))
    return any(not isinstance(node.cause, ast.Constant) for node in raises)


def _drops_its_cause(handler: ast.ExceptHandler, module: str) -> bool:
    if not _is_broad(handler):
        return False
    carried = _reads_what_it_caught(handler) or _keeps_it_as_context(handler)
    return not carried and _says_it_failed(handler, module)


def _dropping_handlers(path: Path, module: str) -> list[str]:
    """Where `path`, read as the module named `module`, drops a cause."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    return [f"{path.name}:{handler.lineno}" for handler in handlers if _drops_its_cause(handler, module)]


def test_no_broad_handler_answers_with_a_verdict_it_cannot_explain() -> None:
    paths = sorted(SCRIPTS.glob("*.py"))
    found = [site for path in paths for site in _dropping_handlers(path, path.name)]
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
    assert _dropping_handlers(module, "shapes.py") == ["shapes.py:4", "shapes.py:9", "shapes.py:14"]


ANSWER_PATH_SHAPES = (
    "def graph():\n"
    "    try:\n        return backend()\n"
    "    except Exception:\n        return None, False, False, 'graph_error'\n"
    "def rerank(trace):\n"
    "    try:\n        return order()\n"
    "    except Exception:\n        trace.fallback_reason = 'reranker_error'\n"
    "def resolve():\n"
    "    try:\n        return resolver()\n"
    "    except Exception:\n        return (), NavigationStatus.ERROR\n"
    "def structural(self):\n"
    "    try:\n        self.values = candidates()\n"
    "    except Exception:\n        self.structural_failed = True\n"
    "def parser():\n"
    "    try:\n        return load()\n"
    "    except Exception:\n        return None\n"
    "def close(handle):\n"
    "    try:\n        handle.close()\n"
    "    except Exception:\n        pass\n"
    "def named():\n"
    "    try:\n        return load()\n"
    "    except Exception as exc:\n        return None, describe_error(exc)\n"
    "def hook():\n"
    "    try:\n        return run()\n"
    "    except Exception:\n        _safe_write_error(traceback.format_exc())\n"
    "def cleanup():\n"
    "    try:\n        return run()\n"
    "    except BaseException:\n        _discard_failed_preparation()\n        raise\n"
)


def test_the_guard_refuses_the_answer_path_fallbacks_that_hid_the_cause(tmp_path: Path) -> None:
    """The reranker, graph, navigation and code-graph handlers, as they stood.

    A failure code in a tuple or an assignment, a failure status attribute, and a
    value fallback in an answer module are refused; a cleanup `pass`, a handler that
    reads the exception and one that reads it through `traceback` are not. Outside
    an answer module a silent value fallback is not refused.
    """
    module = tmp_path / "shapes.py"
    module.write_text(ANSWER_PATH_SHAPES, encoding="utf-8")

    in_answer_module = _dropping_handlers(module, "code_graph.py")
    elsewhere = _dropping_handlers(module, "shapes.py")

    named = ["shapes.py:4", "shapes.py:9", "shapes.py:14", "shapes.py:19"]
    assert (in_answer_module, elsewhere) == ([*named, "shapes.py:24"], named)
