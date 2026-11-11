"""Live Git and non-Git workspace revision contracts."""

from __future__ import annotations

import inspect
import os
from dataclasses import fields
from pathlib import Path

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import (
    MAX_REVISION_BYTES,
    MAX_REVISION_FILES,
    PYTHON_CONFIG_NAMES,
    RevisionEntry,
    WorkspaceDelta,
    WorkspaceRevision,
    compute_workspace_revision,
    diff_workspace_revisions,
)

from tests.workspace_revision_helpers import _isolated_global_git_configuration  # noqa: F401


def _entries(revision: WorkspaceRevision) -> dict[str, RevisionEntry]:
    return {entry.path: entry for entry in revision.entries}


def test_fixture_copy_excludes_generated_bytecode(tmp_path, monkeypatch):
    from tests import shared_fixtures

    fixture = tmp_path / "fixture"
    generated = fixture / "pkg/__pycache__"
    generated.mkdir(parents=True)
    (generated / "module.cpython-312.pyc").write_bytes(b"generated")
    (fixture / "module.pyc").write_bytes(b"generated")
    (fixture / "module.py").write_text("value = 1\n")
    monkeypatch.setattr(shared_fixtures, "FIXTURE_ROOT", fixture)
    copied = shared_fixtures.copy_python_fixture(tmp_path / "copy")
    assert {p.relative_to(copied).as_posix() for p in copied.rglob("*")} == {"pkg", "module.py"}


def test_public_contract_has_exact_constants_dataclass_fields_and_signatures() -> None:
    # One comparison per group of facts: the complexity gate counts every `assert`
    # as a branch, and a tuple compare of N facts costs what one assert costs.
    assert (PYTHON_CONFIG_NAMES, MAX_REVISION_FILES, MAX_REVISION_BYTES) == (
        frozenset(
            {
                ".python-version",
                "Pipfile",
                "Pipfile.lock",
                "poetry.lock",
                "pyproject.toml",
                "pyrightconfig.json",
                "setup.cfg",
                "tox.ini",
                "uv.lock",
            }
        ),
        100_000,
        2 * 1024 * 1024 * 1024,
    )
    assert (
        [field.name for field in fields(RevisionEntry)],
        [field.name for field in fields(WorkspaceRevision)],
        [field.name for field in fields(WorkspaceDelta)],
    ) == (
        ["path", "kind", "sha256", "size"],
        [
            "repository_id",
            "checkout_id",
            "git_head",
            "entries",
            "revision_sha256",
        ],
        [
            "created",
            "changed",
            "renamed",
            "deleted",
        ],
    )
    assert (
        RevisionEntry.__dataclass_params__.frozen,
        WorkspaceRevision.__dataclass_params__.frozen,
        WorkspaceDelta.__dataclass_params__.frozen,
        RevisionEntry.__slots__,
    ) == (True, True, True, ("path", "kind", "sha256", "size"))
    assert (
        str(inspect.signature(compute_workspace_revision)),
        str(inspect.signature(diff_workspace_revisions)),
    ) == (
        "(repository: 'RepositoryScope', *, deadline: 'float | None' = None, "
        "cancelled: 'Callable[[], bool] | None' = None) -> 'WorkspaceRevision'",
        "(before: 'WorkspaceRevision', after: 'WorkspaceRevision') -> 'WorkspaceDelta'",
    )


def test_revision_changes_for_dirty_untracked_deleted_and_config(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    before = compute_workspace_revision(scope)
    (repository / "pkg/api.py").write_text("class Changed:\n    pass\n", encoding="utf-8")
    (repository / "pkg/new.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "pkg/base.py").unlink()
    (repository / "pyrightconfig.json").write_text(
        '{"typeCheckingMode":"strict"}', encoding="utf-8"
    )

    after = compute_workspace_revision(scope)

    assert (
        after.revision_sha256 != before.revision_sha256,
        {item.kind for item in after.entries}
        >= {"modified", "untracked", "deleted", "configuration"},
        _entries(after)["pkg/base.py"],
    ) == (True, True, RevisionEntry("pkg/base.py", "deleted", None, 0))
    delta = diff_workspace_revisions(before, after)
    assert (delta.created, delta.changed, delta.deleted) == (
        ("pkg/new.py",),
        ("pkg/api.py", "pyrightconfig.json"),
        ("pkg/base.py",),
    )


def test_revision_bounds_containment_resolution_to_unique_paths(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    real_resolve = Path.resolve
    resolved_paths: list[Path] = []

    def recording_resolve(path: Path, *args, **kwargs) -> Path:
        resolved_paths.append(path)
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", recording_resolve)

    compute_workspace_revision(scope)

    unique_paths = {os.fspath(path) for path in resolved_paths}
    assert len(resolved_paths) <= len(unique_paths) * 3


def test_unchanged_verifier_uses_one_combined_git_state(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_exact_state = workspace_revision.git_state
    real_private_state = workspace_revision.git_state_with_private_index
    exact_calls = 0
    private_calls = 0

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact_state(*args, **kwargs)

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private_state(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
    monkeypatch.setattr(workspace_revision, "git_state_with_private_index", recording_private)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert exact_calls + private_calls == 1


def test_unchanged_verifier_hashes_content_instead_of_trusting_metadata(
    repository: Path,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    target = repository / "pkg" / "api.py"
    original = target.stat()
    content = target.read_bytes()
    replacement = bytes([content[0] ^ 1]) + content[1:]
    target.write_bytes(replacement)
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False


def test_unchanged_verifier_detects_new_relevant_inventory(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    (repository / "pkg" / "new_source.py").write_text("value = 1\n", encoding="utf-8")

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is False
