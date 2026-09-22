"""A thing gets a page only when it recurs, and the page grows by dated pointer lines, by code.

Sun et al. 2023: consolidate only what recurs. Zhang et al. 2026: a page a model
rewrites every night falls below no memory at all. So the ledger's recurrence gate
opens on the second day, and the page is extended through the recoverable Markdown
transaction with lines code wrote. Research:
`docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fact_keys  # noqa: E402
import ledger  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402

DAYS = ("2023-03-02", "2023-03-10")


def _daily(day: str) -> str:
    return (
        f"# Daily Session Memory — {day}\n\n## [10:00:00] session_end | s-{day}\n\n"
        f"_captured: {day} 10:00:00_\n\n**user:** I cleaned my road bike chain on {day}.\n\n"
        "**assistant:** Noted.\n"
    )


def _reply(prompt: str, system: str) -> str:
    """The same record for every turn of the batch: one road bike, chain cleaned."""
    records = [{"kind": "bike", "thing": "road bike", "event": "chain cleaned"}]
    turn = {"facts": ["I cleaned my road bike chain"], "records": records}
    return json.dumps({str(index): turn for index in range(prompt.count("<turn id="))})


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    for name in ("daily", "notes", "projects"):
        (root / "knowledge" / name).mkdir(parents=True)
    for day in DAYS:
        (root / "knowledge" / "daily" / f"{day}.md").write_bytes(_daily(day).encode("utf-8"))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    return root


def _keyed_store(vault: Path, tmp_path: Path, days: tuple[str, ...]) -> fact_keys.KeyStore:
    paths = [f"knowledge/daily/{day}.md" for day in days]
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=paths)
    store = fact_keys.KeyStore(tmp_path / "state" / "cache" / "fact-keys" / "keys.sqlite3")
    fact_keys.key_turns(store, snapshot.chunks, _reply)
    return store


def test_one_day_opens_no_page(vault: Path, tmp_path: Path) -> None:
    store = _keyed_store(vault, tmp_path, DAYS[:1])

    changed = ledger.extend_entity_pages(vault, store.connection, "2023-03-03")
    store.close()

    assert (changed, sorted((vault / "knowledge" / "notes").iterdir())) == (0, [])


def test_the_second_day_creates_the_page_with_both_pointers_and_a_rerun_adds_nothing(
    vault: Path, tmp_path: Path
) -> None:
    store = _keyed_store(vault, tmp_path, DAYS)

    created = ledger.extend_entity_pages(vault, store.connection, "2023-03-11")
    again = ledger.extend_entity_pages(vault, store.connection, "2023-03-12")
    store.close()
    page = (vault / "knowledge" / "notes" / "ledger-bike-road-bike.md").read_text(encoding="utf-8")

    assert (created, again) == (1, 0)
    assert page.startswith("---\ntype: entity\n")
    assert [line[2:12] for line in page.splitlines() if line.startswith("- 2023")] == list(DAYS)
