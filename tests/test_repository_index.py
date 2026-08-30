"""CODE-03: generations for repositories other than the vault.

Every test here builds a real Git repository in a temp directory and drives the
real code path. The one thing deliberately not exercised end to end is a full
`index_repository` build in every case -- that costs an embedding-model load --
so the build is exercised once, and the admission, root-selection, listing and
detection paths are exercised on their own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


def _repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


ALPHA = "def helper(value):\n    return value + 1\n\n\ndef caller(value):\n    return helper(value) * 2\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """An isolated vault and state root; nothing here touches the live one."""
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    import memory_state

    monkeypatch.setattr(memory_state, "ROOT", root, raising=False)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    return root, state


# ---------------------------------------------------------------- admission


def test_a_path_that_is_not_a_git_repository_is_refused_by_name(vault, tmp_path):
    import repository_index

    _root, state = vault
    plain = tmp_path / "plain"
    (plain / "scripts").mkdir(parents=True)

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.admit_repository(plain, state_root=state)

    assert refusal.value.reason == "repository_not_git"


def test_a_directory_inside_a_repository_is_refused_and_names_the_root(
    vault, tmp_path
):
    """cbm registers `/home/user/llm-wiki/scripts` as a peer of the repository
    that contains it. A subdirectory is not a repository."""
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.admit_repository(repository / "scripts", state_root=state)

    assert refusal.value.reason == "repository_not_checkout_root"
    # The details carry the canonical serialisation, which on Windows has an
    # upper-case drive and forward slashes. Compare as a path, or this asserts
    # the spelling rather than the root.
    assert Path(refusal.value.details["checkout_root"]) == repository.resolve()


def test_a_symlinked_path_is_refused_and_offers_the_real_path(vault, tmp_path):
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    link = tmp_path / "link"
    link.symlink_to(repository, target_is_directory=True)

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.admit_repository(link, state_root=state)

    assert refusal.value.reason == "repository_path_is_symlinked"
    assert refusal.value.details["real_path"] == str(repository)


def test_a_submodule_is_refused_and_names_its_superproject(vault, tmp_path):
    import repository_index

    _root, state = vault
    inner = _repository(tmp_path / "inner", {"scripts/alpha.py": ALPHA})
    outer = _repository(tmp_path / "outer", {"scripts/beta.py": ALPHA})
    subprocess.run(
        ["git", "-C", str(outer), "-c", "protocol.file.allow=always",
         "submodule", "add", "-q", str(inner), "vendor"],
        check=True,
        capture_output=True,
    )
    _git(outer, "commit", "-qm", "add submodule")

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.admit_repository(outer / "vendor", state_root=state)

    assert refusal.value.reason == "repository_is_submodule"
    assert refusal.value.details["superproject"] == str(outer)


def test_the_vault_itself_is_refused(vault, tmp_path):
    import repository_index

    root, state = vault
    _repository(root, {"scripts/alpha.py": ALPHA})

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.admit_repository(root, state_root=state)

    assert refusal.value.reason == "repository_is_the_vault"


@pytest.mark.skipif(os.name != "posix", reason="ownership is a POSIX boundary")
def test_a_repository_owned_by_another_user_is_refused(vault, tmp_path, monkeypatch):
    """There is no second user on this machine, so the euid is what moves.

    The boundary under test is ownership, not identity, and it is reached the
    same way either way: `st_uid` does not equal the caller's effective uid.
    """
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(repository).st_uid + 1)

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.admit_repository(repository, state_root=state)

    assert refusal.value.reason == "repository_not_owned_by_caller"


def test_a_repository_owned_by_the_caller_passes_the_ownership_gate(vault, tmp_path):
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})

    admission = repository_index.admit_repository(repository, state_root=state)

    assert admission.root == repository
    assert admission.ownership_checked is (os.name == "posix")


def test_a_missing_directory_is_refused_before_anything_is_opened(vault, tmp_path):
    import repository_index

    _root, state = vault

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.admit_repository(tmp_path / "absent", state_root=state)

    assert refusal.value.reason == "repository_path_not_a_directory"


# -------------------------------------------------------------- code roots


def test_tracked_top_level_entries_ignore_untracked_build_output(tmp_path):
    """The tracked set is the point: it excludes caches and virtualenvs for free.

    `top.py` is in the answer and `.venv` is not, which is the whole claim: the
    unit is the tracked entry, not the tracked directory.
    """
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {"scripts/alpha.py": ALPHA, "docs/guide.md": "# Guide\n", "top.py": "x = 1\n"},
    )
    (repository / ".venv/lib").mkdir(parents=True)
    (repository / ".venv/lib/huge.py").write_text("y = 2\n", encoding="utf-8")

    assert repository_index.tracked_top_level_entries(repository) == (
        "docs",
        "scripts",
        "top.py",
    )


def test_a_foreign_layout_is_indexed_whole_not_refused_for_its_names(tmp_path):
    """`src/` is not this vault's name, and that is not a reason to refuse it."""
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {"src/alpha.py": ALPHA, "lib/beta.py": ALPHA, "cmd/main.go": "package main\n"},
    )

    roots = repository_index.selected_code_roots(repository, None)

    assert roots.selected == ("cmd", "lib", "src")
    assert roots.excluded == ()


def test_a_tracked_hidden_directory_is_excluded_by_name_not_silently(tmp_path):
    """Every real repository tracks `.github`; the walk prunes it everywhere.

    It is left out rather than refused, because the corpus walk already refuses
    to descend into a hidden directory in *this* vault too -- but the omission
    is carried out, not swallowed.
    """
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {"src/alpha.py": ALPHA, ".github/workflows/ci.yml": "on: push\n"},
    )

    roots = repository_index.selected_code_roots(repository, None)

    assert roots.selected == ("src",)
    assert roots.excluded == (".github",)


def test_a_repository_of_only_pruned_directories_refuses_by_name(tmp_path):
    """Nothing collectable is a refusal, and the refusal says what was pruned."""
    import repository_index

    repository = _repository(
        tmp_path / "repo", {".github/workflows/ci.yml": "on: push\n"}
    )

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.selected_code_roots(repository, None)

    assert refusal.value.reason == "repository_has_no_code_roots"
    assert refusal.value.details["excluded_roots"] == [".github"]


def test_an_explicitly_requested_pruned_root_is_refused_by_name(tmp_path):
    """`.claude/worktrees/` is a second copy of the repository -- commit 1d06e6a."""
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {"src/alpha.py": ALPHA, ".claude/worktrees/agent-a/src/alpha.py": ALPHA},
    )

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.selected_code_roots(repository, ["src", ".claude"])

    assert refusal.value.reason == "repository_root_not_collectable"
    assert refusal.value.details["refused_roots"] == [".claude"]
    assert refusal.value.details["admissible_roots"] == ["src"]


def test_explicit_roots_accept_a_deliberately_narrower_index(tmp_path):
    import repository_index

    repository = _repository(
        tmp_path / "repo", {"src/alpha.py": ALPHA, "scripts/beta.py": ALPHA}
    )

    roots = repository_index.selected_code_roots(repository, ["scripts"])

    assert roots.selected == ("scripts",)
    assert roots.excluded == ()


def test_an_explicit_root_that_does_not_exist_is_refused(tmp_path):
    import repository_index

    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.selected_code_roots(repository, ["scripts", "nowhere"])

    assert refusal.value.reason == "repository_root_missing"
    assert refusal.value.details["missing_roots"] == ["nowhere"]


# ------------------------------------------------------- index, list, detect


_RECEIPT_FIELDS = (
    "status",
    "activated",
    "code_roots",
    "sources",
    # The walk under a root is a filesystem walk, not a Git listing. The
    # receipt says so, and counts what that cost, rather than leaving a reader
    # to infer that "indexed" meant "tracked".
    "source_selection",
    "untracked_sources",
)


def _assert_receipt(receipt: dict) -> None:
    assert {name: receipt[name] for name in _RECEIPT_FIELDS} == {
        "status": "indexed",
        "activated": False,
        "code_roots": ["scripts"],
        "sources": 1,
        "source_selection": "filesystem-walk",
        "untracked_sources": 0,
    }


def _assert_pointer_untouched_and_scope_resolves(state, repository, receipt) -> None:
    import generation_catalog
    from repository_scope import resolve_repository_scope

    catalog = generation_catalog.GenerationCatalog(state)
    assert catalog.get_active() is None, "a foreign index must not move the pointer"
    selected = catalog.get_active_for_repository(resolve_repository_scope(repository))
    assert selected is not None
    assert selected["generation_id"] == receipt["generation_id"]


def _assert_navigation_reads_the_generation(repository, receipt) -> None:
    from code_graph import find_callers

    answer = find_callers("helper", repository, with_report=True)
    assert answer["fallback"] is False
    assert answer["source_generation"] == receipt["generation_id"]
    assert [row["qualified_name"] for row in answer["callers"]] == [
        "scripts.alpha.caller"
    ]


def _assert_listing(state, repository) -> None:
    import repository_index

    rows = repository_index.list_repositories(state_root=state)["repositories"]
    assert [row["checkout_root"] for row in rows] == [str(repository)]
    assert rows[0]["code_roots"] == ["scripts"]
    assert rows[0]["active"] is False


def _assert_detects_nothing_then_an_edit(state, repository) -> None:
    import repository_index

    unchanged = repository_index.detect_repository_changes(repository, state_root=state)
    assert unchanged["stale"] is False
    assert unchanged["counts"] == {"added": 0, "removed": 0, "modified": 0}
    (repository / "scripts/alpha.py").write_text(
        ALPHA + "\n# edited\n", encoding="utf-8"
    )
    (repository / "scripts/gamma.py").write_text(
        "def added():\n    return 1\n", encoding="utf-8"
    )
    changed = repository_index.detect_repository_changes(repository, state_root=state)
    _assert_change_report(changed)


def _assert_change_report(changed: dict) -> None:
    assert changed["stale"] is True
    assert changed["counts"] == {"added": 1, "removed": 0, "modified": 1}
    assert changed["added"] == ["scripts/gamma.py"]
    assert changed["modified"] == ["scripts/alpha.py"]


def test_a_second_repository_is_indexed_registered_and_never_activated(
    vault, tmp_path
):
    """The whole CODE-03 contract in one test.

    Before this change the catalog had one active pointer and
    `get_active_for_repository` answered None for any other repository, so the
    second repository could not be resolved at all.
    """
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})

    receipt = repository_index.index_repository(repository, state_root=state)

    _assert_receipt(receipt)
    _assert_pointer_untouched_and_scope_resolves(state, repository, receipt)
    _assert_navigation_reads_the_generation(repository, receipt)
    _assert_listing(state, repository)
    _assert_detects_nothing_then_an_edit(state, repository)


def test_detect_says_not_indexed_rather_than_pretending_nothing_changed(
    vault, tmp_path
):
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})

    report = repository_index.detect_repository_changes(repository, state_root=state)

    assert report["status"] == "not_indexed"
    assert report["stale"] is True


def test_listing_an_empty_catalog_does_not_create_one(vault):
    import repository_index

    _root, state = vault

    listing = repository_index.list_repositories(state_root=state)

    assert listing["repositories"] == []
    assert not (state / "cache/evidence-graph/catalog.sqlite3").exists()


# ---------------------------------------------------------- the MCP boundary


def test_the_three_verbs_are_modes_of_get_architecture_not_a_thirteenth_tool():
    import mcp_server

    schema = mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]
    modes = set(schema["properties"]["mode"]["enum"])

    assert {"index", "repositories", "changes"} <= modes
    assert len(mcp_server._build_tool_definitions()) in (0, 12)


def _mode_contract_errors() -> list:
    import mcp_server

    return [
        (mode in mcp_server._ARCHITECTURE_CONTRACTS)
        and mcp_server._validate_architecture_arguments(
            {"directory": "/tmp", "mode": mode}
        )
        for mode in ("index", "repositories", "changes")
    ]


def test_every_new_mode_declares_its_contract_and_rejects_stray_arguments():
    import mcp_server

    assert _mode_contract_errors() == [None, None, None]
    assert mcp_server._validate_architecture_arguments(
        {"directory": "/tmp", "mode": "changes", "roots": ["scripts"]}
    ) == "arguments are not valid for changes: roots"
    assert (
        mcp_server._validate_architecture_arguments(
            {"directory": "/tmp", "mode": "index", "roots": ["scripts"]}
        )
        is None
    )


def test_the_index_mode_argument_passes_the_shared_object_schema():
    """The new argument goes through `_validate_object_schema`, not around it."""
    import mcp_server

    schema = mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]

    assert (
        mcp_server._validate_object_schema(
            schema, {"directory": "/tmp", "mode": "index", "roots": ["scripts"]}
        )
        is None
    )
    assert (
        mcp_server._validate_object_schema(
            schema, {"directory": "/tmp", "mode": "index", "roots": "scripts"}
        )
        == "argument 'roots' must be an array"
    )
    assert (
        mcp_server._validate_object_schema(
            schema, {"directory": "/tmp", "mode": "index", "roots": [1]}
        )
        == "argument 'roots' items must be strings"
    )


def test_indexing_gets_a_measured_budget_and_the_other_modes_do_not():
    import mcp_server

    assert mcp_server._tool_operation_seconds(
        "get_architecture", {"directory": "/tmp", "mode": "index"}
    ) == mcp_server.MCP_REPOSITORY_INDEX_SECONDS
    assert (
        mcp_server._tool_operation_seconds(
            "get_architecture", {"directory": "/tmp", "mode": "repositories"}
        )
        == mcp_server.MCP_OPERATION_SECONDS
    )


def test_a_refusal_reaches_the_caller_as_a_named_answer_not_an_exception(
    vault, tmp_path
):
    import mcp_server

    _root, state = vault
    plain = tmp_path / "plain"
    plain.mkdir()

    data = mcp_server._architecture_tool_call(
        {"directory": str(plain), "mode": "index"}, deadline=None
    )

    assert data["status"] == "refused"
    assert data["reason"] == "repository_not_git"


def test_an_index_answer_is_not_labelled_a_live_extraction_fallback():
    """It extracts nothing, so the graph-completeness warning would be false."""
    import mcp_server

    quality = mcp_server._quality_of_code_graph(
        "get_architecture",
        {"schema_version": "repository-index/v1", "status": "ok", "repositories": []},
        {},
        False,
    )

    assert quality["fallback"] is False
    assert quality["warnings"] == []
    refused = mcp_server._quality_of_code_graph(
        "get_architecture",
        {
            "schema_version": "repository-index/v1",
            "status": "refused",
            "reason": "repository_not_git",
        },
        {},
        False,
    )
    assert refused["partial"] is True
    assert refused["warnings"] == ["Refused: repository_not_git"]


def test_an_ordinary_graph_answer_keeps_its_existing_envelope():
    import mcp_server

    quality = mcp_server._quality_of_code_graph(
        "get_architecture",
        {"fallback": False, "graph_complete": True, "unresolved_count": 0},
        {},
        False,
    )

    assert quality == {
        "coverage": 0.95,
        "confidence": 0.9,
        "fallback": False,
        "partial": False,
        "warnings": [],
    }


# ------------------------------------------------------------------- catalog


def _two_repository_catalog(tmp_path):
    """One catalog holding a generation for each of two repositories."""
    from repository_scope import resolve_repository_scope

    from tests.test_generation_catalog import _catalog, _publish

    scopes = []
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        scopes.append(resolve_repository_scope(tmp_path / name))
    catalog = _catalog(tmp_path)
    for name, scope in zip(("first", "second"), scopes):
        _publish(catalog, f"{name}-gen", repository_scope=scope.as_dict())
        catalog.register(f"{name}-gen")
    return catalog, scopes


def test_the_catalog_holds_two_repositories_and_answers_each_from_its_own(
    tmp_path,
):
    """The selection change, without paying for two real builds."""
    catalog, (first_scope, second_scope) = _two_repository_catalog(tmp_path)

    assert catalog.activate("first-gen", expected_active=None)

    assert catalog.get_active_for_repository(first_scope)["generation_id"] == "first-gen"
    assert (
        catalog.get_active_for_repository(second_scope)["generation_id"] == "second-gen"
    )
    assert catalog.get_active()["generation_id"] == "first-gen"


def test_registered_manifests_skip_a_row_whose_bytes_stopped_matching(tmp_path):
    """One damaged registration must not hide every healthy one from a listing."""
    import sqlite3
    from contextlib import closing

    from tests.test_generation_catalog import _catalog, _publish

    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    _publish(catalog, "gen-2")
    catalog.register("gen-1")
    catalog.register("gen-2")
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        database.execute(
            "UPDATE generations SET manifest_json = ? WHERE generation_id = 'gen-1'",
            (json.dumps({"tampered": True}).encode("utf-8"),),
        )
        database.commit()

    identifiers = {
        identifier for identifier, _at, _manifest in catalog.registered_manifests()
    }

    assert identifiers == {"gen-2"}
