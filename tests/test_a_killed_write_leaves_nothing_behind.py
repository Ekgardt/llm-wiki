"""A write killed between staging and rename leaves nothing behind (2026-09-26).

docs/research/2026-09-26-a-killed-write-leaves-nothing-behind.md
"""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import reclaim_runtime_state as reclaim

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_NONCES = {"uuid.uuid4().hex": "0" * 32, "os.getpid()": "4242"}


def _rendered(value: ast.FormattedValue) -> str:
    source = ast.unparse(value.value)
    if source.startswith("secrets.token_hex("):
        return "a" * 2 * int(ast.literal_eval(value.value.args[0]))
    return _NONCES.get(source, "name")


def _text(node: ast.JoinedStr) -> str:
    return "".join(part.value if isinstance(part, ast.Constant) else _rendered(part) for part in node.values)


def _staging_names(tree: ast.AST) -> list[tuple[int, str]]:
    """Every f-string that names a `.tmp` file, with its placeholders filled in."""
    rendered = [(node.lineno, _text(node)) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)]
    return [(line, text) for line, text in rendered if text.endswith(".tmp")]


def _unsweepable_lines(path: Path) -> list[int]:
    names = _staging_names(ast.parse(path.read_text(encoding="utf-8")))
    return [line for line, name in names if not reclaim.STAGED_WRITE_NAME.match(name)]


def test_every_staging_name_in_scripts_is_one_the_sweep_recognises() -> None:
    unsweepable = {path.name: _unsweepable_lines(path) for path in sorted(SCRIPTS.glob("*.py"))}

    assert {name: lines for name, lines in unsweepable.items() if lines} == {}


def _aged(path: Path, seconds: float) -> Path:
    path.write_bytes(b"staged")
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def test_the_nightly_removes_a_stale_staged_write_and_keeps_everything_else(tmp_path) -> None:
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    stale = _aged(project / f".journal.md.{'a' * 32}.tmp", 7200)
    fresh = _aged(project / f".journal.md.{'b' * 32}.tmp", 60)
    personal = _aged(project / ".notes.tmp", 7200)

    result = reclaim.sweep_staged_knowledge_writes(tmp_path)

    assert (result["removed"], stale.exists(), fresh.exists(), personal.exists()) == (1, False, True, True)
