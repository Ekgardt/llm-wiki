import lint_memory
import pytest


def _page(tmp_path, monkeypatch, body):
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    page = tmp_path / "note.md"
    page.write_text("---\ntype: concept\nconfidence: high\nsource_authority: user\n---\n# Note\n" + body)
    return page


def test_concise_supported_page_has_no_arbitrary_word_floor(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, "The user's chosen setting is off.\nSource: owner decision.\n")
    assert lint_memory.check_sparse_pages([page], lint_memory.DEFAULT_SPARSE_WORDS) == []
    assert lint_memory.check_missing_sources_section([page]) == []


def test_short_claim_without_source_is_reported(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, "The system guarantees every write is delivered.\n")
    assert lint_memory.check_missing_sources_section([page]) == ["note.md"]


def test_frontmatter_and_title_are_not_body_words(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, "")
    assert lint_memory._word_count(page) == 0
    assert lint_memory.check_sparse_pages([page], lint_memory.DEFAULT_SPARSE_WORDS)


@pytest.mark.parametrize("source", ["Source: ", "Evidence:\n", "Provenance:"])
def test_empty_source_line_does_not_count(tmp_path, monkeypatch, source):
    page = _page(tmp_path, monkeypatch, "A claim.\n" + source)
    assert lint_memory.check_missing_sources_section([page]) == ["note.md"]


@pytest.mark.parametrize("label", ["Source", "Evidence", "Provenance"])
def test_contract_source_line_counts(tmp_path, monkeypatch, label):
    page = _page(tmp_path, monkeypatch, f"A claim.\n{label}: `docs/decision.md`.\n")
    assert lint_memory.check_missing_sources_section([page]) == []


def test_explicit_operator_word_floor_is_still_enforced(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, "A concise note.\n")
    assert lint_memory.check_sparse_pages([page], 200)
