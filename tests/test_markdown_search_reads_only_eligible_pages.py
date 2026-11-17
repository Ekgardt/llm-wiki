from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import generation_search
import pytest
from llm_wiki import runtime_roots
from llm_wiki.generation_catalog_store import GenerationCatalog

search: Any = generation_search

PAGE = "---\ntype: concept\nproject: lantern\n{extra}---\n# {title}\n\nA page about lanterns.\n"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    return root


def _page(directory: Path, name: str, extra: str = "") -> Path:
    page = directory / name
    page.write_text(PAGE.format(extra=extra, title=page.stem), encoding="utf-8")
    return page


def _notes(vault: Path) -> Path:
    return vault / "knowledge" / "notes"


def test_an_unknown_scope_or_a_missing_tree_collects_nothing(tmp_path: Path) -> None:
    assert generation_search.collect_pages("elsewhere") == []
    missing = tmp_path / "vault" / "knowledge" / "notes"
    assert generation_search.collect_pages("all", knowledge_dir=missing) == []


def test_an_unreadable_subdirectory_is_skipped(vault: Path) -> None:
    _page(_notes(vault), "lantern.md")
    locked = _notes(vault) / "locked"
    locked.mkdir()
    _page(locked, "hidden.md")
    locked.chmod(0)
    try:
        pages = generation_search.collect_pages("all", knowledge_dir=_notes(vault), root=vault)
    finally:
        locked.chmod(0o700)
    assert [page.name for page in pages] == ["lantern.md"]


def test_skipped_retired_and_oversized_pages_are_not_collected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page(_notes(vault), "index.md")
    _page(_notes(vault), "retired.md", extra="status: superseded\n")
    _page(_notes(vault), "kept.md")
    oversized = _notes(vault) / "oversized.md"
    oversized.write_text("x" * 400, encoding="utf-8")
    monkeypatch.setattr(generation_search, "MAX_PAGE_BYTES", 300)
    pages = generation_search.collect_pages("all", knowledge_dir=_notes(vault), root=vault)
    assert [page.name for page in pages] == ["kept.md"]


def test_more_pages_than_the_limit_are_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page(_notes(vault), "first.md")
    _page(_notes(vault), "second.md")
    monkeypatch.setattr(generation_search, "setting_value", lambda _name: 1)
    with pytest.raises(ValueError, match="searchable page limit exceeded"):
        generation_search.collect_pages("all", knowledge_dir=_notes(vault), root=vault)


def test_a_missing_or_linked_entry_is_not_safe(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("text", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    assert not generation_search._is_safe_entry(tmp_path / "missing.md", directory=False)
    assert not generation_search._is_safe_entry(link, directory=False)


def test_a_knowledge_root_outside_or_under_a_link_is_refused(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    real = tmp_path / "real-notes"
    real.mkdir()
    vault.mkdir()
    (vault / "notes").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError, match="escapes the vault"):
        generation_search._require_safe_ancestry(real, vault)
    with pytest.raises(OSError, match="unsafe knowledge directory"):
        generation_search._require_safe_ancestry(vault / "notes", vault)


@pytest.mark.parametrize(
    ("extra", "as_of", "valid"),
    [
        ("", "2026-06-01", True),
        ("valid_to: null\n", "2026-06-01", True),
        ("valid_to: 2026-01-01\n", "2026-06-01", False),
        ("valid_to: 2026-12-01\n", "2026-06-01", True),
    ],
)
def test_a_page_is_valid_until_its_valid_to_day(
    vault: Path, extra: str, as_of: str, valid: bool
) -> None:
    page = _page(_notes(vault), "dated.md", extra=extra)
    assert generation_search._valid_as_of(str(page), as_of) is valid
    assert generation_search._valid_as_of("knowledge/notes/dated.md", as_of) is valid


def test_a_page_that_cannot_be_read_is_valid_by_default(vault: Path) -> None:
    assert generation_search._valid_as_of(str(_notes(vault) / "absent.md"), "2026-06-01")


def test_a_page_outside_the_vault_yields_no_hit(vault: Path, tmp_path: Path) -> None:
    outside = _page(tmp_path, "outside.md")
    assert generation_search.read_page(outside, "search page") is None
    assert generation_search._exact_page_hit(outside, project=None, since=None, as_of=None) is None
    hit = generation_search._direct_page_hit(
        outside, query_terms={"lanterns"}, project=None, since=None, as_of=None
    )
    assert hit is None


def test_an_exact_page_of_another_project_yields_no_hit(vault: Path) -> None:
    page = _page(_notes(vault), "lantern.md")
    assert generation_search._exact_page_hit(page, project="other", since=None, as_of=None) is None
    recalled = generation_search._recalled_retired_hit(
        "lantern", project="other", since=None, as_of=None
    )
    assert recalled is None
    found = generation_search._exact_page_hit(page, project="LANTERN", since=None, as_of=None)
    assert found is not None


@pytest.mark.parametrize(
    ("project", "since", "eligible"),
    [("other", None, False), (None, "2026-06-01", False), ("Lantern", "2026-01-01", True)],
)
def test_exact_page_eligibility_follows_project_and_since(
    project: str | None, since: str | None, eligible: bool
) -> None:
    assert (
        generation_search._exact_page_eligible(
            "knowledge/notes/lantern.md",
            page_project="lantern",
            timestamp="2026-03-01",
            project=project,
            since=since,
            as_of=None,
        )
        is eligible
    )


def test_a_question_without_words_finds_no_markdown(vault: Path) -> None:
    hits = generation_search.direct_markdown_hits(
        "?!",
        [_page(_notes(vault), "lantern.md")],
        limit=5,
        project=None,
        since=None,
        as_of=None,
        deadline=None,
        cancelled=None,
    )
    assert hits == []


def test_given_pages_are_used_as_they_are(tmp_path: Path) -> None:
    given = [tmp_path / "given.md"]
    assert generation_search._resolved_pages(given, "all", None) is given


def test_notes_scopes_filter_on_the_notes_tree_and_the_validity_window() -> None:
    clauses, values = generation_search.generation_filters(
        scope="wiki", since="2026-01-01T00:00:00", as_of="2026-06-01T00:00:00"
    )
    assert "knowledge/notes/" in clauses
    assert values == ["2026-06-01", "2026-06-01", "2026-01-01"]


@pytest.mark.parametrize(
    ("row", "outside"),
    [
        ({}, False),
        ({"relative_path": "knowledge/notes/a.md"}, False),
        ({"path": "knowledge\\projects\\a.md"}, True),
    ],
)
def test_notes_scopes_keep_only_pages_under_the_notes_tree(
    row: dict[str, object], outside: bool
) -> None:
    assert generation_search._out_of_scope(row, "wiki") is outside


def test_a_result_of_the_asked_project_is_boosted() -> None:
    result: dict[str, object] = {"score": 1.0, "project": "Lantern", "title": "other words"}
    assert generation_search._boosted_generation_score(result, {"lantern"}, "lantern") == 2.0
    assert generation_search._same_project("Lantern", "lantern")


def test_a_repeated_degradation_is_announced_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(generation_search, "DEGRADATIONS", {})
    monkeypatch.setattr(generation_search, "_degradations_announced", set())
    generation_search.note_degradation("dense", RuntimeError("first"))
    generation_search.note_degradation("dense", RuntimeError("second"))
    assert capsys.readouterr().err.count("dense degraded") == 1
    assert "second" in generation_search.DEGRADATIONS["dense"]


@pytest.mark.parametrize("deadline", [True, float("inf")])
def test_a_publication_deadline_must_be_finite(deadline: float) -> None:
    with pytest.raises(ValueError, match="finite monotonic timestamp"):
        generation_search._require_finite_deadline(deadline)


def test_a_live_scope_needs_a_repository_scope_expectation(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="must be a RepositoryScope"):
        search._resolved_live_scope(tmp_path, object(), deadline=None, cancelled=None)


def test_an_existing_catalog_is_opened_and_a_broken_one_is_not(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = tmp_path / "state" / "cache" / "evidence-graph" / "catalog.sqlite3"
    assert generation_search.active_generation_catalog() is None
    GenerationCatalog(tmp_path / "state")
    assert generation_search.active_generation_catalog() is not None
    catalog_path.unlink()
    catalog_path.write_bytes(b"not a database at all, only bytes" * 4)
    assert generation_search.active_generation_catalog() is None
    assert runtime_roots.state_root() == tmp_path / "state"


def test_an_artifact_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="must be a regular file"):
        generation_search._require_regular_file(tmp_path)


def test_an_artifact_replaced_before_it_was_opened_is_refused(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    with (
        first.open("rb") as source,
        pytest.raises(PermissionError, match="changed while opening"),
    ):
        generation_search._hashed_open_file(source, second.stat(), None, None)


def test_a_seal_past_its_deadline_stops_the_reader(tmp_path: Path) -> None:
    catalog = SimpleNamespace(generations_path=tmp_path)
    with pytest.raises(TimeoutError):
        generation_search.generation_consumption_seal(
            catalog, {"generation_id": "g1"}, (), deadline=time.monotonic() - 1
        )
    with pytest.raises(TimeoutError):
        generation_search.generation_consumption_unchanged(
            catalog, {"generation_id": "g1"}, (), (), deadline=time.monotonic() - 1
        )


def test_a_manifest_without_a_named_generation_cannot_be_sealed(tmp_path: Path) -> None:
    catalog = SimpleNamespace(generations_path=tmp_path)
    manifest: dict[str, object] = {"generation_id": 5, "artifacts": []}
    assert generation_search.generation_consumption_seal(catalog, manifest, ()) is None


def test_an_active_generation_that_is_not_a_manifest_is_not_the_sealed_one() -> None:
    assert not generation_search._same_sealed_generation(
        object(), None, {}, (), (), deadline=None, cancelled=None
    )


def test_a_manifest_without_a_named_search_index_is_not_connectable() -> None:
    assert not generation_search._connectable_generation({}, None, "g1")
    assert generation_search.generation_connection(SimpleNamespace(), {}) is None


@pytest.mark.parametrize("artifacts", ["search.sqlite3", [{"path": "other"}]])
def test_an_unnamed_artifact_has_no_digest(artifacts: object) -> None:
    assert generation_search._named_artifact_digest(artifacts, "search.sqlite3") == ""


@pytest.mark.parametrize("entries", [["search.sqlite3"], ()])
def test_seal_entries_without_exactly_one_search_index_name_none(entries: object) -> None:
    assert generation_search._named_fts_seal(entries) is None


def _chunks_database() -> sqlite3.Connection:
    columns = generation_search._GENERATION_CHUNK_COLUMNS
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute(f"CREATE TABLE chunks ({columns})")
    database.execute(
        "INSERT INTO chunks (chunk_id, chunk_order, source_path, project) VALUES (?, ?, ?, ?)",
        ("c1", 0, "knowledge/notes/lantern.md", "Lantern"),
    )
    generation_search._register_filename_stem_function(database)
    return database


@pytest.mark.parametrize(("project", "found"), [("lantern", ["c1"]), ("other", [])])
def test_an_exact_filename_match_honours_the_asked_project(project: str, found: list[str]) -> None:
    with closing(_chunks_database()) as database:
        rows = generation_search._exact_filename_rows(database, "lantern", "", [], project)
    assert [row["chunk_id"] for row in rows] == found


def test_a_manifest_without_a_repository_scope_is_not_unchanged(tmp_path: Path) -> None:
    catalog = SimpleNamespace(generations_path=tmp_path)
    assert not generation_search.generation_consumption_unchanged(catalog, {}, (), ())


def test_a_linked_page_is_not_collected(vault: Path, tmp_path: Path) -> None:
    _page(_notes(vault), "kept.md")
    (_notes(vault) / "linked.md").symlink_to(_page(tmp_path, "outside.md"))
    pages = generation_search.collect_pages("all", knowledge_dir=_notes(vault), root=vault)
    assert [page.name for page in pages] == ["kept.md"]
