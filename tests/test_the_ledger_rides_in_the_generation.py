"""The records the fact-keys call posts ride into the generation, where one call counts them.

The nightly keying is the one provider call the ledger reuses; the generation
build copies its store into a `ledger` table of `search.sqlite3` exactly as it
copies the keys, and the reader's one call, `ledger.count_in_active_generation`,
reads the active generation with no provider and no token. Research:
`docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import corpus_snapshot  # noqa: E402
import fact_keys  # noqa: E402
import ledger  # noqa: E402
import search_memory  # noqa: E402

DAILY = "knowledge/daily/2023-03-20.md"
BODY = (
    "# Daily Session Memory — 2023-03-20\n\n"
    "## [07:32:00] session_end | s1\n\n_captured: 2023-03-20 07:32:00_\n\n"
    "**user:** I'm looking into getting a new tire for my commuter bike this month.\n\n"
    "**assistant:** Noted.\n\n"
    "## [18:33:00] session_end | s2\n\n_captured: 2023-03-20 18:33:00_\n\n"
    "**user:** I got my road bike serviced at Pedal Power on March 10th.\n\n"
    "**assistant:** Noted.\n"
)
REPLY = {
    "0": {
        "facts": ["I am replacing the front tire of my commuter bike"],
        "records": [{"kind": "bike", "thing": "commuter bike", "event": "front tire replaced"}],
    },
    "1": {
        "facts": ["I had my road bike serviced at Pedal Power on March 10th"],
        "records": [
            {"kind": "bike", "thing": "road bike", "event": "serviced", "date": "2023-03-10"}
        ],
    },
}


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for name in ("daily", "notes", "projects"):
        (root / "knowledge" / name).mkdir(parents=True)
    (root / DAILY).write_bytes(BODY.encode("utf-8"))
    return root


def _keyed(state: Path, snapshot) -> int:
    store = fact_keys.KeyStore(fact_keys.store_path(state))
    try:
        return fact_keys.key_turns(store, snapshot.chunks, lambda prompt, system: json.dumps(REPLY))
    finally:
        store.close()


def _built(tmp_path: Path, vault: Path, snapshot):
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    catalog = GenerationCatalog(tmp_path / "state")
    sources = [
        {
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }
        for source in snapshot.sources
    ]
    return build_full_generation(
        catalog,
        sources=sources,
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="ledger-rides",
        snapshot=snapshot,
        publication_root=vault,
        repository_scope=resolve_repository_scope(vault),
    )


def test_the_keying_call_posts_records_and_the_generation_carries_them(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    state = tmp_path / "state"
    snapshot = corpus_snapshot.collect_corpus(vault, daily_paths=(DAILY,))
    keyed = _keyed(state, snapshot)

    built = _built(tmp_path, vault, snapshot)
    counted = ledger.count_in_active_generation("bikes", state_root=state)

    assert (keyed, built.activated) == (2, True)
    assert (counted.records, counted.things, counted.events, counted.tier) == (2, 2, 2, ledger.PROBABLE)
    assert {pointer.day for pointer in counted.pointers} == {"2023-03-10", "2023-03-20"}


def test_the_artifact_with_a_ledger_table_still_validates_as_a_search_artifact(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    snapshot = corpus_snapshot.collect_corpus(vault, daily_paths=(DAILY,))
    directory = tmp_path / "generation"
    directory.mkdir()
    rows = [ledger.Record("bike", "road bike", "", "2023-03-20", None, True, False, DAILY, "", 1, 2, "e" * 64).row()]

    search_memory.build_generation_fts(snapshot, directory, ledger_rows=rows)

    with sqlite3.connect(directory / search_memory.GENERATION_FTS_ARTIFACT) as database:
        valid = search_memory._valid_fts_schema(database)
        counted = ledger.count(database, "bike")
    assert (valid, counted.things) == (True, 1)


def test_a_build_that_carried_no_ledger_leaves_no_table_behind(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    snapshot = corpus_snapshot.collect_corpus(vault, daily_paths=(DAILY,))
    directory = tmp_path / "generation"
    directory.mkdir()

    search_memory.build_generation_fts(snapshot, directory)

    with sqlite3.connect(directory / search_memory.GENERATION_FTS_ARTIFACT) as database:
        assert ledger.count(database, "bike") is None


def test_a_vault_without_a_generation_answers_none(tmp_path: Path) -> None:
    assert ledger.count_in_active_generation("bike", state_root=tmp_path / "empty") is None
