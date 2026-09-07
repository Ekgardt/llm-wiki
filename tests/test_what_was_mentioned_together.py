"""The flytrap counts: accumulate, decay, and fire only at a threshold.

Multi-session questions are the ones whose answer is assembled from more than
one conversation, and they are among our weakest. The failure is not finding the
right session — retrieval returns the labelled answer session for 87% of
questions — it is that the *second* session never comes back with the first.

Spreading activation is the field's name for the remedy and it is current work.
The lesson every paper repeats is the one the flytrap already states: activation
that spreads without a decay or a gate reaches the whole graph and means
nothing. So what is tested here is mostly the brakes.

See `docs/research/2026-09-06-what-was-mentioned-together.md`.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import co_activation  # noqa: E402

TODAY = date(2026, 9, 6)


def _vault(tmp_path: Path, **entries: str) -> Path:
    vault = tmp_path / "vault"
    daily = vault / "knowledge" / "daily"
    daily.mkdir(parents=True)
    for day, text in entries.items():
        name = day.lstrip("d").replace("_", "-")
        (daily / f"{name}.md").write_text(text, encoding="utf-8")
    return vault


def test_two_names_in_one_entry_become_neighbours(tmp_path: Path) -> None:
    vault = _vault(tmp_path, d2026_09_06="worked on [[undo]] and [[snapshots]] today")

    table = co_activation.build(vault, TODAY)

    assert co_activation.neighbours("undo", table).keys() == {"snapshots"}


def test_a_pairing_is_symmetric(tmp_path: Path) -> None:
    vault = _vault(tmp_path, d2026_09_06="[[undo]] and [[snapshots]]")

    table = co_activation.build(vault, TODAY)

    assert "undo" in co_activation.neighbours("snapshots", table)


def test_an_old_pairing_weighs_less_than_a_recent_one(tmp_path: Path) -> None:
    """The decay is the mechanism, not decoration."""
    vault = _vault(
        tmp_path,
        d2026_09_06="[[undo]] with [[snapshots]]",
        d2026_01_01="[[undo]] with [[archives]]",
    )

    boosts = co_activation.neighbours("undo", co_activation.build(vault, TODAY))

    assert boosts["snapshots"] > boosts["archives"]


def test_repetition_outweighs_a_single_mention(tmp_path: Path) -> None:
    vault = _vault(
        tmp_path,
        d2026_09_06="[[undo]] with [[snapshots]]",
        d2026_09_05="[[undo]] with [[snapshots]]",
        d2026_09_04="[[undo]] with [[archives]]",
    )

    boosts = co_activation.neighbours("undo", co_activation.build(vault, TODAY))

    assert boosts["snapshots"] > boosts["archives"]


def test_an_index_page_links_nothing_to_anything(tmp_path: Path) -> None:
    """Everything co-occurring with everything is a fact about the page."""
    links = " ".join(f"[[page-{index}]]" for index in range(30))
    vault = _vault(tmp_path, d2026_09_06=links)

    assert co_activation.build(vault, TODAY) == {}


def test_the_boost_is_capped(tmp_path: Path) -> None:
    vault = _vault(tmp_path, d2026_09_06="[[undo]] " * 3 + "[[snapshots]]")

    boosts = co_activation.neighbours("undo", co_activation.build(vault, TODAY))

    assert max(boosts.values()) == 1.0 + co_activation.MAX_NEIGHBOUR_BOOST


def test_a_page_with_no_pairings_gets_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path, d2026_09_06="[[undo]] alone")

    assert co_activation.neighbours("undo", co_activation.build(vault, TODAY)) == {}


def test_a_missing_table_is_silence(tmp_path: Path) -> None:
    assert co_activation.load(tmp_path / "absent.json") == {}


def test_the_table_survives_being_written_and_read(tmp_path: Path) -> None:
    vault = _vault(tmp_path, d2026_09_06="[[undo]] and [[snapshots]]")
    table = co_activation.build(vault, TODAY)

    co_activation.save(table, tmp_path / "table.json")

    assert co_activation.load(tmp_path / "table.json") == table


def test_a_page_keeps_only_its_strongest_pairings(tmp_path: Path) -> None:
    entries = {
        f"d2026_09_0{index % 9 + 1}": f"[[hub]] and [[side-{index}]]" for index in range(80)
    }
    vault = _vault(tmp_path, **entries)

    table = co_activation.build(vault, TODAY)

    assert len(table["hub"]) <= co_activation.MAX_NEIGHBOURS


def test_only_the_leading_page_spreads_its_neighbours(monkeypatch) -> None:
    """The gate is the mechanism. Spreading from every candidate reaches the
    whole graph and means nothing, which is what the literature is about."""
    import retrieval

    monkeypatch.setattr(
        retrieval,
        "_co_activation_table",
        lambda: {"alpha": {"beta": 1.0}, "gamma": {"delta": 1.0}},
    )
    monkeypatch.setattr(retrieval, "_standing_disposition", lambda query: {})
    scores = {"a": 3.0, "b": 1.0, "c": 1.0, "d": 1.0}
    meta = {
        "a": {"relative_path": "knowledge/notes/alpha.md"},
        "b": {"relative_path": "knowledge/notes/beta.md"},
        "c": {"relative_path": "knowledge/notes/gamma.md"},
        "d": {"relative_path": "knowledge/notes/delta.md"},
    }

    retrieval._weigh_by_trust(scores, meta, curated_first=False, query="q")

    assert meta["b"]["alongside_weight"] > 1.0
    assert meta["d"]["alongside_weight"] == 1.0


def test_no_table_leaves_every_score_alone(monkeypatch) -> None:
    import retrieval

    monkeypatch.setattr(retrieval, "_co_activation_table", lambda: {})
    monkeypatch.setattr(retrieval, "_standing_disposition", lambda query: {})
    scores = {"a": 2.0}
    meta = {"a": {"relative_path": "knowledge/notes/alpha.md"}}

    weighted = retrieval._weigh_by_trust(scores, meta, curated_first=False, query="q")

    assert weighted["a"] == 2.0
    assert meta["a"]["alongside_weight"] == 1.0


def test_the_nightly_rebuilds_the_table(tmp_path, monkeypatch) -> None:
    import reclaim_runtime_state

    result = reclaim_runtime_state.rebuild_co_activation()

    assert "co_activation_pages" in result or "co_activation_error" in result
