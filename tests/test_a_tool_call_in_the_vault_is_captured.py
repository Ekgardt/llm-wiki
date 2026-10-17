"""A tool call in the vault is captured, and no module runs on a stand-in import.

The tool hook skipped every call made inside the vault, and the capture hooks
replaced a failed import with a function that did nothing. See
docs/research/2026-09-25-a-tool-call-in-the-vault-is-captured.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

import capture_diagnostics
import post_tool_capture
import user_prompt_capture

from tests.test_breadcrumb_worker import ingress as ingress
from tests.test_capture_hooks import _published_content

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def test_a_tool_call_made_inside_the_vault_is_durably_captured(ingress) -> None:
    import json

    hook = {"tool_name": "Edit", "tool_input": {"file_path": "scripts/x.py"}, "cwd": str(ingress[1].vault)}
    post_tool_capture._capture_tool(hook)
    payload = json.loads(_published_content(ingress[1]))['payload']
    assert payload['tool_name'] == 'Edit' and payload['target'] == 'scripts/x.py'


def test_the_capture_hooks_record_failures_with_the_real_recorder() -> None:
    real = capture_diagnostics.record_capture_failure

    assert (post_tool_capture.record_capture_failure, user_prompt_capture.record_capture_failure) == (real, real)


def _handler_statements(node: ast.Try) -> list[ast.stmt]:
    return [statement for handler in node.handlers for statement in handler.body]


def _defines_a_stand_in(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Try):
        return False
    return any(isinstance(item, (ast.FunctionDef, ast.ClassDef)) for item in _handler_statements(node))


def _stand_ins(path: Path) -> list[int]:
    """Module-level `try: import` whose handler defines a function or class in its place."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in tree.body if _defines_a_stand_in(node)]


def test_no_module_replaces_a_failed_import_with_a_stand_in() -> None:
    found = {path.name: _stand_ins(path) for path in sorted(SCRIPTS.glob("*.py"))}

    assert {name: lines for name, lines in found.items() if lines} == {}
