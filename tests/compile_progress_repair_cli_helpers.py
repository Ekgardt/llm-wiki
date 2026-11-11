"""Helpers shared by test_compile_progress_repair_cli.py and the test files importing them."""
from __future__ import annotations


def _arguments(root, state, image):
    return (
        "--apply", "--upgrade-compile-progress", "--backup-image", str(image),
        "--confirm-all-agents-stopped", "--root", str(root),
        "--state-root", str(state), "--json",
    )
