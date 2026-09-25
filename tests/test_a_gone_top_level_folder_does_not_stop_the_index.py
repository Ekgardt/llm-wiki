"""A removed top-level folder is a removal, and a new one is named, not silently skipped.

The refresh passed the first index's roots back as an explicit request, so a
deleted or renamed top-level folder refused every detect and refresh. See
docs/research/2026-09-25-a-gone-top-level-folder-does-not-stop-the-index.md.
"""

from __future__ import annotations

import shutil

from tests.test_repository_index import ALPHA, _git, _repository
from tests.test_repository_refresh import adopted_vault  # noqa: F401 - fixture


def test_a_removed_root_is_refreshed_and_a_new_one_is_named(adopted_vault, tmp_path) -> None:  # noqa: F811
    import repository_index

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA, "tools/beta.py": ALPHA})
    repository_index.index_repository(repository, state_root=state)
    shutil.rmtree(repository / "tools")
    (repository / "extra").mkdir()
    (repository / "extra" / "gamma.py").write_text(ALPHA, encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "move")

    detected = repository_index.detect_repository_changes(repository, state_root=state)
    refreshed = repository_index.refresh_repository(repository, state_root=state)

    assert (detected["stale"], detected["uncovered_roots"]) == (True, ["extra"])
    assert (refreshed["status"], refreshed["code_roots"]) == ("refreshed", ["scripts"])
