"""The reuse gates ask identity, so a commit alone no longer defeats them.

Found while verifying NEW-111 on 2026-08-29. NEW-111, NEW-90 and NEW-65 were
all one mistake: a recorded ``RepositoryScope`` compared whole against a
live-resolved one. A scope carries ``git_commit``, which is build-time
provenance, and this vault commits its own runtime — so the comparison means
"almost never".

Two sites made it, and both are fixed (NEW-138, 2026-08-29):

* ``evidence_graph_builder._reuse_config_matches`` — the gate behind commit
  ``283eb3a`` ("idle pass 643s to 3.9s"). Refusing it meant the first build
  after any commit reused no records at all.
* ``doctor._parent_matches_identity`` — while ``doctor._scope_state`` a
  thousand lines earlier already documented the distinction and answered
  ``superseded``.

Both now ask ``repository_scope.same_repository_record``, the serialized-form
twin of ``RepositoryScope.same_repository``. These four tests were pinned here
as ``xfail(strict=True)`` while the files belonged to other agents; the fix
turned both pins XPASS, and they are ordinary passing tests from 2026-08-29.
Each still ships with its control, so a regression in the gate itself shows up
as the control failing rather than as a silent pass.

Evidence: docs/research/2026-08-29-new-111-was-fixed-before-it-was-filed.md
and docs/research/2026-08-29-one-definition-of-repository-identity.md
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

COMMIT_BUILT = "a" * 40
COMMIT_NOW = "b" * 40


def _scope_dict(commit: str) -> dict[str, str]:
    """One repository and checkout, differing only in the commit.

    The identities must be really derived from the paths: `RepositoryScope`
    validates that on load, and `str(Path)` yields backslashes on Windows that
    the canonical drive-letter form refuses — so serialize through the
    product's own canonicaliser, as `test_open_active_scope_identity` does.
    """
    from repository_scope import (
        _local_serialized_path,
        derive_checkout_id,
        derive_repository_id,
    )

    root = _local_serialized_path(Path(__file__).resolve().parent / "vault", strict=False)
    common = _local_serialized_path(
        Path(__file__).resolve().parent / "vault" / ".git", strict=False
    )
    repository_id = derive_repository_id(checkout_root=root, git_common_dir=common)
    return {
        "schema_version": "repository-scope/v1",
        "repository_id": repository_id,
        "checkout_id": derive_checkout_id(repository_id, root),
        "checkout_root": root,
        "git_common_dir": common,
        "git_commit": commit,
    }


def _reuse_config():
    from evidence_graph_builder import IncrementalReuseConfig

    return IncrementalReuseConfig(
        extractor_version="graph-extractor/v1",
        grammar_version="grammar/v1",
        compiler_version="compiler/v1",
        resolver_config_sha256="c" * 64,
        schema_version="evidence-graph/v2",
        workspace_manifest_sha256="d" * 64,
    )


def _parent_manifest(config) -> dict[str, object]:
    import evidence_graph_builder

    return {
        "version": evidence_graph_builder.INCREMENTAL_MANIFEST_VERSION,
        "reuse_config": asdict(config),
    }


def _reuse_allowed(built_commit: str, current_commit: str) -> bool:
    import evidence_graph_builder

    config = _reuse_config()
    generation = {"repository_scope": _scope_dict(built_commit)}
    return evidence_graph_builder._reuse_config_matches(
        _parent_manifest(config), generation, config, _scope_dict(current_commit)
    )


def test_the_reuse_gate_holds_when_nothing_moved():
    """The control: identical scopes are admitted, so the gate itself works."""
    assert _reuse_allowed(COMMIT_BUILT, COMMIT_BUILT) is True


def test_the_reuse_gate_survives_a_commit_that_moved_nothing_else():
    """Same repository, same checkout, later commit — reuse must still apply."""
    assert _reuse_allowed(COMMIT_BUILT, COMMIT_NOW) is True


class _FakeSnapshot:
    """Only the two version fields `_parent_matches_versions` reads."""

    collector_version = "collector/v1"
    extractor_version = "extractor/v1"


def _doctor_parent(built_commit: str) -> dict[str, object]:
    return {
        "schema_version": "corpus-generation/v2",
        "repository_scope": _scope_dict(built_commit),
        "collector_version": "collector/v1",
        "extractor_version": "extractor/v1",
        "graph_extractor_version": "graph-extractor/v1",
    }


def _doctor_identity_matches(built_commit: str, current_commit: str) -> bool:
    import doctor
    from repository_scope import RepositoryScope

    return doctor._parent_matches_identity(
        _doctor_parent(built_commit),
        RepositoryScope.from_dict(_scope_dict(current_commit)),
        _FakeSnapshot(),
        "graph-extractor/v1",
    )


def test_doctor_identity_holds_when_nothing_moved():
    """The control for the doctor site."""
    assert _doctor_identity_matches(COMMIT_BUILT, COMMIT_BUILT) is True


def test_doctor_identity_survives_a_commit_that_moved_nothing_else():
    """`_scope_state` calls this case `superseded`; `_parent_matches_identity` should agree."""
    assert _doctor_identity_matches(COMMIT_BUILT, COMMIT_NOW) is True


def test_a_record_that_names_no_repository_matches_nothing():
    """Two unusable records are not evidence of the same repository.

    The whole-record `==` this replaces said two empty dicts were the same
    checkout. `record_identity` has no identity to offer for a record that
    names neither a repository nor a checkout, so it matches nothing --
    including another record just as damaged.
    """
    from repository_scope import record_identity, same_repository_record

    assert record_identity({}) is None
    assert record_identity("not a scope") is None
    assert same_repository_record({}, {}) is False
    assert same_repository_record({}, _scope_dict(COMMIT_NOW)) is False


def test_absence_agrees_only_with_absence():
    """A build with no scope reuses a parent with no scope, and nothing else."""
    from repository_scope import same_repository_record

    assert same_repository_record(None, None) is True
    assert same_repository_record(None, _scope_dict(COMMIT_NOW)) is False
    assert same_repository_record(_scope_dict(COMMIT_NOW), None) is False


def test_the_object_form_and_the_serialized_form_give_one_answer():
    """`RepositoryScope.identity` and `record_identity` read one field list."""
    from repository_scope import IDENTITY_FIELDS, RepositoryScope, record_identity

    record = _scope_dict(COMMIT_BUILT)
    scope = RepositoryScope.from_dict(record)
    assert record_identity(record) == scope.identity() == record_identity(scope)
    assert "git_commit" not in IDENTITY_FIELDS


def test_a_different_checkout_is_still_refused():
    """Identity is weaker than equality, not absent: another checkout is not this one."""
    from repository_scope import same_repository_record

    here = _scope_dict(COMMIT_BUILT)
    elsewhere = {**here, "checkout_id": "checkout:" + "e" * 64}
    assert same_repository_record(here, elsewhere) is False
