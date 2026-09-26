"""On a `core.autocrlf` checkout an edit names its own symbol, exactly (audit 2026-09-26 B-8).

docs/research/2026-09-26-a-line-ending-is-not-an-edit.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from impact_analysis import _changed_ranges, analyze_impact

COMMITTED = b"def alpha():\n    return 1\n\n\ndef beta():\n    return 1\n"
CHECKED_OUT = COMMITTED.replace(b"\n", b"\r\n")


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, timeout=60)


def _crlf_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    for key, value in (("user.email", "t@example.test"), ("user.name", "T"), ("core.autocrlf", "false")):
        _git(root, "config", key, value)
    (root / "mod.py").write_bytes(COMMITTED)
    _git(root, "add", "mod.py")
    _git(root, "commit", "-qm", "initial")
    _git(root, "config", "core.autocrlf", "true")
    (root / "mod.py").unlink()
    _git(root, "checkout", "--", "mod.py")
    return root


def _occurrence(start: int, end: int, first: int, last: int) -> list[dict]:
    return [{"relative_path": "mod.py", "byte_start": start, "byte_end": end, "line_start": first, "line_end": last}]


class _CrlfGeneration:
    """The generation indexed the CRLF worktree, as the indexer reads it."""

    generation_id = "generation-crlf"
    nodes = {
        name: {"node_id": name, "kind": "function", "identity_key": name, "metadata": {"name": name, "path": "mod.py"}}
        for name in ("alpha", "beta")
    }
    spans = {
        "alpha": _occurrence(0, CHECKED_OUT.index(b"\r\n\r\n") + 2, 1, 2),
        "beta": _occurrence(CHECKED_OUT.index(b"def beta"), len(CHECKED_OUT), 5, 6),
    }

    def find_nodes(self, *, path=None, **_options):
        return list(self.nodes.values()) if path == "mod.py" else []

    def source_by_path(self, relative_path, **_options):
        return {"relative_path": relative_path, "content": CHECKED_OUT} if relative_path == "mod.py" else None

    def occurrences(self, node_id, **_options):
        return self.spans.get(node_id, [])

    def edges(self, **_options):
        return []

    def node(self, node_id):
        return self.nodes.get(node_id)

    def evidence(self, **_options):
        return []


def test_an_edit_on_a_crlf_checkout_names_only_its_symbol_exactly(tmp_path) -> None:
    root = _crlf_checkout(tmp_path)
    assert (root / "mod.py").read_bytes() == CHECKED_OUT
    (root / "mod.py").write_bytes(CHECKED_OUT.replace(b"def beta():\r\n    return 1", b"def beta():\r\n    return 2"))

    report = analyze_impact(root=root, graph=_CrlfGeneration(), textual_fallback=False)

    assert [(item["name"], item["classification"]) for item in report["changed_symbols"]] == [("beta", "exact")]


def test_a_change_of_line_endings_alone_is_still_a_change() -> None:
    assert _changed_ranges(COMMITTED, CHECKED_OUT) != []


def test_only_line_endings_apart_is_no_edit_inside_other_changes() -> None:
    edited = CHECKED_OUT.replace(b"def beta():\r\n    return 1", b"def beta():\r\n    return 2")

    ranges = _changed_ranges(COMMITTED, edited)

    assert [(item["new"]["line_start"], item["new"]["line_end"]) for item in ranges] == [(6, 6)]
