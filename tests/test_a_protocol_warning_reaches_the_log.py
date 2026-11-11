"""A language-server protocol warning reaches stderr, not a callback nobody set."""

from __future__ import annotations

import ast

from llm_wiki import lsp_process_generation

from tests.script_sources import split_module_files


def _is_protocol_construction(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "LspProtocol"


def _protocol_constructions() -> list[ast.Call]:
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in split_module_files("lsp_process", "lsp_process")]
    return [node for tree in trees for node in ast.walk(tree) if _is_protocol_construction(node)]


def _unwired(call: ast.Call) -> bool:
    return "warning_callback" not in {keyword.arg for keyword in call.keywords}


def test_every_production_protocol_is_given_a_warning_callback() -> None:
    calls = _protocol_constructions()
    unwired = [call.lineno for call in filter(_unwired, calls)]
    assert calls and not unwired, f"LspProtocol without warning_callback: {unwired or 'none built'}"


def test_the_warning_is_written_to_stderr(capsys) -> None:
    lsp_process_generation.report_protocol_warning("server request failed: ValueError")
    assert capsys.readouterr().err == "llm-wiki lsp: server request failed: ValueError\n"
