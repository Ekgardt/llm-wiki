"""Structured data is redacted as structure, never as its serialized text (audit 2026-09-27 A-4).

docs/research/2026-09-27-a-secret-in-structure-is-redacted-as-structure.md
"""
from __future__ import annotations

import ast
from pathlib import Path

import secret_redact
from event_envelope import build_event_envelope

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_an_event_payload_blanks_a_secret_named_key() -> None:
    envelope = build_event_envelope(
        event_type="post_tool_use", payload={"tool_name": "Bash", "target": "run", "token": "abc"},
        redact=secret_redact.redact_secrets,
    )

    assert (envelope.payload["token"], envelope.payload["target"]) == ("[REDACTED]", "run")


def _redacts_serialized_json(node: ast.AST) -> bool:
    """`redact_secrets(json.dumps(...))`: a regex over the text of a structure."""
    if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "redact_secrets":
        return False
    return any("json.dumps(" in ast.unparse(argument) for argument in node.args)


def _offending_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree) if _redacts_serialized_json(node)]


def test_no_script_redacts_serialized_json_as_text() -> None:
    offenders = {path.name: _offending_lines(path) for path in sorted(SCRIPTS.glob("*.py"))}

    assert {name: lines for name, lines in offenders.items() if lines} == {}
