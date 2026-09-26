"""Every git call has a deadline; the snapshot's commits ignore the operator's global config.

Audit 2026-09-26 C-13. docs/research/2026-09-26-every-git-call-has-a-deadline.md
"""
from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_WAITING_CALLS = frozenset({"run", "check_output", "check_call", "call"})


def _starts_git(node: ast.Call) -> bool:
    if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
        return False
    elements = node.args[0].elts
    return bool(elements) and isinstance(elements[0], ast.Constant) and elements[0].value == "git"


def _waiting_call(node: ast.AST) -> bool:
    """`subprocess.run(...)` and the other calls that wait for the child."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in _WAITING_CALLS


def _untimed_git(node: ast.AST) -> bool:
    if not _waiting_call(node) or not _starts_git(node):
        return False
    return "timeout" not in {keyword.arg for keyword in node.keywords}


def test_no_script_waits_on_git_without_a_timeout() -> None:
    found = [
        f"{path.name}:{node.lineno}"
        for path in sorted(SCRIPTS.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if _untimed_git(node)
    ]

    assert found == []


def test_a_global_signing_config_does_not_reach_the_snapshot(tmp_path, monkeypatch) -> None:
    import snapshot_knowledge

    config = tmp_path / "global.gitconfig"
    config.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    vault = tmp_path / "vault"
    (vault / "knowledge").mkdir(parents=True)
    (vault / "knowledge" / "page.md").write_text("# page\n", encoding="utf-8")

    result = snapshot_knowledge.take_snapshot(vault, tmp_path / "copy")

    assert (result["status"], result["commit"] not in {None, "no change"}) == ("ok", True)
