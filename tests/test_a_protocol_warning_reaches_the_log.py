"""A language-server protocol warning reaches stderr, not a callback nobody set."""

from __future__ import annotations

import ast
from pathlib import Path

import lsp_process

ROOT = Path(__file__).resolve().parents[1]


def _protocol_constructions() -> list[ast.Call]:
    tree = ast.parse((ROOT / "scripts" / "lsp_process.py").read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "LspProtocol"
    ]


def _unwired(call: ast.Call) -> bool:
    return "warning_callback" not in {keyword.arg for keyword in call.keywords}


def test_every_production_protocol_is_given_a_warning_callback() -> None:
    calls = _protocol_constructions()
    unwired = [call.lineno for call in filter(_unwired, calls)]
    assert calls and not unwired, f"LspProtocol without warning_callback: {unwired or 'none built'}"


def test_the_warning_is_written_to_stderr(capsys) -> None:
    lsp_process._report_protocol_warning("server request failed: ValueError")
    assert capsys.readouterr().err == "llm-wiki lsp: server request failed: ValueError\n"
