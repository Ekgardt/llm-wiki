"""Helpers shared by test_a_short_page_still_needs_evidence.py and the test files importing them."""



def _page(tmp_path, monkeypatch, body):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    page = tmp_path / "note.md"
    page.write_text("---\ntype: concept\nconfidence: high\nsource_authority: user\n---\n# Note\n" + body)
    return page
