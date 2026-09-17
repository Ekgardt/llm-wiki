"""Each contradiction run reads the next window of pages and says what it did not read.

Research: `docs/research/2026-09-17-the-contradiction-audit-moves-through-the-vault.md`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CLEAN = "NO_CONTRADICTIONS"
SILENT = ""
NOT_RUN = "(contradiction check did not run: no provider answered)"

# Own interpreter: the cursor lives in runtime state, which this test must own.
# One run per reply. An empty reply stands for a provider that does not answer:
# a local Ollama on a closed loopback port, refused at once, nothing leaves the host.
RUNS = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import lint_memory
pages = sorted((lint_memory.ROOT / "knowledge" / "notes").glob("*.md"))
os.environ["MEMORY_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
runs = []
for reply in json.loads(sys.argv[3]):
    os.environ["MEMORY_LLM_PROVIDER"] = "fake" if reply else "ollama"
    os.environ["MEMORY_LLM_FAKE_RESPONSE"] = reply
    runs.append(lint_memory.check_contradictions(pages, max_bytes=int(sys.argv[2])))
print(json.dumps(runs))
"""


def _runs(tmp_path: Path, sizes: dict[str, int], replies: list[str]) -> list[list[str]]:
    notes = tmp_path / "vault" / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (tmp_path / "state" / "run").mkdir(parents=True)
    for name, size in sizes.items():
        (notes / name).write_text("x" * size, encoding="utf-8")
    environment = {
        **os.environ,
        "LLM_WIKI_ROOT": str(tmp_path / "vault"),
        "LLM_WIKI_STATE_ROOT": str(tmp_path / "state"),
        "MEMORY_LLM_PROVIDER": "fake",
    }
    done = subprocess.run(
        [sys.executable, "-c", RUNS, str(SCRIPTS), "800", json.dumps(replies)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def _note(read: int, total: int, first: str, following: str) -> str:
    return (
        f"(read {read} of {total} pages this run, from knowledge/notes/{first}; "
        f"the next run continues at knowledge/notes/{following})"
    )


def test_three_runs_read_three_different_windows_and_come_round(tmp_path):
    sizes = {"a.md": 300, "b.md": 300, "c.md": 300, "d.md": 300, "e.md": 300}

    runs = _runs(tmp_path, sizes, [CLEAN, CLEAN, CLEAN])

    assert runs == [
        [_note(2, 5, "a.md", "c.md")],
        [_note(2, 5, "c.md", "e.md")],
        [_note(2, 5, "e.md", "b.md")],
    ]


def test_a_run_no_provider_answered_does_not_move_the_window(tmp_path):
    sizes = {"a.md": 300, "b.md": 300, "c.md": 300}

    runs = _runs(tmp_path, sizes, [SILENT, CLEAN])

    assert runs == [[NOT_RUN], [_note(2, 3, "a.md", "c.md")]]


def test_a_page_larger_than_one_call_is_named_and_does_not_end_the_audit(tmp_path):
    runs = _runs(tmp_path, {"a.md": 5000, "b.md": 100, "c.md": 100}, [CLEAN])

    assert runs == [["(never read, larger than one call: knowledge/notes/a.md)"]]


def test_a_vault_of_pages_too_large_to_read_is_not_called_clean(tmp_path):
    runs = _runs(tmp_path, {"a.md": 5000}, [CLEAN])

    assert runs == [["(never read, larger than one call: knowledge/notes/a.md)"]]


def test_a_vault_that_fits_one_call_is_read_whole_and_says_nothing_more(tmp_path):
    runs = _runs(tmp_path, {"a.md": 100, "b.md": 100}, [CLEAN, CLEAN])

    assert runs == [[], []]
