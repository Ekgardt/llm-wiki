"""A text is redacted whole, then cut: no secret is judged by a fragment (audit 2026-09-27 B-4).

docs/research/2026-09-27-a-secret-is-redacted-before-it-is-cut.md
"""
from __future__ import annotations

import ast
from pathlib import Path

import integration_adapter
import session_evidence

from tests.script_sources import script_files, script_key

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
REDACTORS = {"redact_secrets", "redact_jsonl", "redact_structure"}


def test_a_subagent_report_cut_across_a_token_shows_no_part_of_it() -> None:
    text = "x" * (session_evidence.MAX_SUBAGENT_REPORT_CHARS - 20) + " " + TOKEN + " tail"

    clipped = session_evidence.clipped_report(text)

    assert TOKEN[:12] not in clipped


def test_a_raw_window_cut_inside_a_token_drops_the_fragment() -> None:
    line = ('{"command":"echo ' + TOKEN + '"}').encode()
    cut_inside = line.index(TOKEN.encode()) + 12

    head = integration_adapter.whole_tokens_head(line[:cut_inside])
    tail = integration_adapter.whole_tokens_tail(line[cut_inside:])

    assert (TOKEN[:12].encode() in head, TOKEN[12:].encode() in tail) == (False, False)


def _is_redactor_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) in REDACTORS


def _is_slice(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)


def _cuts_before_redacting(node: ast.AST) -> bool:
    """A redactor handed `text[:n]`: the cut came first."""
    if not _is_redactor_call(node):
        return False
    return any(_is_slice(item) for argument in node.args for item in ast.walk(argument))


def _offending_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree) if _cuts_before_redacting(node)]


def test_no_script_redacts_a_text_it_already_cut() -> None:
    offenders = {script_key(path): _offending_lines(path) for path in script_files()}

    assert {name: lines for name, lines in offenders.items() if lines} == {}
