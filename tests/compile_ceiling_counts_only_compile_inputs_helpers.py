"""Helpers shared by test_compile_ceiling_counts_only_compile_inputs.py and the test files importing them."""

from pathlib import Path


def _bind_compiler(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
