"""One odd entry under a code root is skipped by name, not a refusal of the whole repository.

Audit 2026-09-26 A-8, docs/research/2026-09-26-one-entry-does-not-refuse-a-repository.md.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_repository_index import ALPHA, _repository, vault  # noqa: F401

pytestmark = pytest.mark.skipif(os.name != "posix", reason="links and byte names are POSIX here")


def _odd_repository(tmp_path: Path) -> Path:
    repository = _repository(
        tmp_path / "repo",
        {"src/alpha.py": ALPHA, "web/app.ts": "export const a = 1;\n", ".gitignore": "node_modules/\n"},
    )
    (repository / "src" / "linked.py").symlink_to(repository / "src" / "alpha.py")
    (repository / "src" / "huge.py").write_bytes(b"#" * (8 * 1024 * 1024 + 1))
    (repository / "web" / "node_modules" / "dep").mkdir(parents=True)
    (repository / "web" / "node_modules" / "dep" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (repository / "web" / "node_modules" / ".bin").mkdir()
    (repository / "web" / "node_modules" / ".bin" / "dep").symlink_to("../dep/index.js")
    _byte_named_file(repository / "src")
    return repository


def _byte_named_file(directory: Path) -> bool:
    """A name that is not UTF-8, where the file system allows one (APFS refuses: EILSEQ)."""
    try:
        with open(os.path.join(os.fsencode(directory), b"caf\xe9.py"), "wb") as handle:
            handle.write(b"x = 1\n")
    except OSError:
        return False
    return True


def test_a_link_a_huge_file_a_byte_name_and_a_nested_ignored_folder_leave_the_rest_indexed(vault, tmp_path):  # noqa: F811
    import repository_index

    _root, state = vault
    repository = _odd_repository(tmp_path)

    receipt = repository_index.index_repository(repository, roots=["src", "web"], state_root=state)

    byte_name = any(name.startswith("src/caf") for name in receipt["skipped_examples"])
    assert (receipt["status"], receipt["sources"], receipt["skipped_entries"]) == ("indexed", 2, 2 + byte_name)
    assert "src/huge.py" in receipt["skipped_examples"]


def test_the_same_repository_is_detected_unchanged(vault, tmp_path):  # noqa: F811
    import repository_index

    _root, state = vault
    repository = _odd_repository(tmp_path)
    repository_index.index_repository(repository, roots=["src", "web"], state_root=state)

    detected = repository_index.detect_repository_changes(repository, state_root=state)

    assert (detected["status"], detected["stale"]) == ("ok", False)


def test_a_code_file_that_is_not_utf8_is_named_not_dropped_in_silence(vault, tmp_path):  # noqa: F811
    """Audit 2026-09-26 B-13."""
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "latin", {"src/alpha.py": ALPHA})
    (repository / "src" / "legacy.py").write_bytes("name = 'café'\n".encode("latin-1"))

    receipt = repository_index.index_repository(repository, roots=["src"], state_root=state)

    assert (receipt["sources"], receipt["skipped_examples"]) == (1, ["src/legacy.py"])
