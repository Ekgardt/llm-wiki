"""The contract says of the index what the code does: it is regenerated, not edited.

The index half of finding M-B5 of the third audit. See
`docs/research/2026-09-18-the-index-is-regenerated-not-edited.md`.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ("CLAUDE.md", "AGENTS.md")


def _contract(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_both_contract_files_send_the_index_through_its_rebuild() -> None:
    told = [
        "rebuild_memory_index.py" in _contract(name)
        and "do\n   not edit it by hand" in _contract(name)
        for name in CONTRACTS
    ]

    assert told == [True, True]


def test_the_two_contract_files_stay_byte_identical() -> None:
    assert (ROOT / "CLAUDE.md").read_bytes() == (ROOT / "AGENTS.md").read_bytes()
