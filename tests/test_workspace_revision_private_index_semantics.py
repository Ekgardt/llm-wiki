"""The private-index fast path of a revision check is taken only when git's configuration, attributes and index cannot change what it reads."""


from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import (
    compute_workspace_revision,
)

from tests.workspace_revision_helpers import (
    _indexed_git_config_variables,
    _isolated_global_git_configuration,  # noqa: F401
    _linux_memfd_available,
    _private_xdg_git_path,
    _run_git,
    _verification_git_call_counts,
)


def _scenario_modified(repository: Path) -> None:
    (repository / "pkg/api.py").write_text("changed = True\n", encoding="utf-8")


def _scenario_untracked(repository: Path) -> None:
    (repository / "pkg/new.py").write_text("new = True\n", encoding="utf-8")


def _scenario_deleted(repository: Path) -> None:
    (repository / "pkg/base.py").unlink()


def _scenario_staged_modification(repository: Path) -> None:
    (repository / "pkg/api.py").write_text("staged = True\n", encoding="utf-8")
    _run_git(repository, "add", "pkg/api.py")


def _scenario_unstaged_rename(repository: Path) -> None:
    (repository / "pkg/rename_target.py").rename(repository / "pkg/renamed.py")


def _scenario_staged_rename(repository: Path) -> None:
    _run_git(repository, "mv", "pkg/rename_target.py", "pkg/renamed.py")


def _scenario_config(repository: Path) -> None:
    (repository / "pyrightconfig.json").write_text(
        '{"typeCheckingMode":"strict"}\n', encoding="utf-8"
    )


def _scenario_ignored_relevant(repository: Path) -> None:
    (repository / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _run_git(repository, "add", ".gitignore")
    _run_git(repository, "commit", "-m", "ignore relevant source")
    (repository / "ignored.py").write_text("ignored = True\n", encoding="utf-8")


_WORKSPACE_SCENARIOS = {
    "modified": _scenario_modified,
    "untracked": _scenario_untracked,
    "deleted": _scenario_deleted,
    "staged-modification": _scenario_staged_modification,
    "unstaged-rename": _scenario_unstaged_rename,
    "staged-rename": _scenario_staged_rename,
    "config": _scenario_config,
    "ignored-relevant": _scenario_ignored_relevant,
}


def _apply_workspace_scenario(repository: Path, scenario: str) -> None:
    """One way a workspace can differ from its recorded revision; "clean" is none."""
    mutate = _WORKSPACE_SCENARIOS.get(scenario)
    if mutate is not None:
        mutate(repository)


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "scenario",
    [
        "clean",
        "modified",
        "untracked",
        "deleted",
        "staged-modification",
        "unstaged-rename",
        "staged-rename",
        "config",
        "ignored-relevant",
    ],
)
def test_private_index_verifier_matches_forced_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    _apply_workspace_scenario(repository, scenario)

    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    private_calls = 0
    real_private = workspace_revision.git_state_with_private_index

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state_with_private_index", recording_private)

    optimized = workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    assert optimized is exact
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_verifier_matches_sha256_fallback_when_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sha256"
    root.mkdir()
    initialized = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", "-b", "main"],
        cwd=root,
        check=False,
        capture_output=True,
        timeout=10,
    )
    if initialized.returncode != 0:
        pytest.skip("Git SHA-256 repositories are unavailable")
    _run_git(root, "config", "user.name", "Revision Test")
    _run_git(root, "config", "user.email", "revision@example.invalid")
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    _run_git(root, "add", "module.py")
    _run_git(root, "commit", "-m", "fixture")
    scope = resolve_repository_scope(root)
    expected = compute_workspace_revision(scope)

    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    private_calls = 0
    real_private = workspace_revision.git_state_with_private_index

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state_with_private_index", recording_private)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is exact
    assert exact is True
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_declines_changed_clean_filter_stale_true(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / ".gitattributes").write_text("pkg/api.py filter=rewrite\n", encoding="ascii")
    _run_git(repository, "config", "filter.rewrite.clean", "cat")
    _run_git(repository, "config", "filter.rewrite.required", "true")
    _run_git(repository, "add", ".gitattributes")
    _run_git(repository, "commit", "-m", "add clean filter")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    _run_git(
        repository,
        "config",
        "filter.rewrite.clean",
        "sed s/PublicApi/PublicApiChanged/",
    )
    target = repository / "pkg/api.py"
    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))

    with monkeypatch.context() as forced:
        forced.setattr(workspace_revision, "try_private_git_state", lambda *args, **kwargs: None)
        exact = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    optimized, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert exact is False
    assert optimized is exact
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "attributes",
    [
        "*.py text\n",
        "*.py text eol=crlf\n",
        "*.py filter=optional\n",
        "*.py ident\n",
        "*.py working-tree-encoding=UTF-8\n",
    ],
)
def test_private_index_declines_worktree_attribute_semantics(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    attributes: str,
) -> None:
    (repository / ".gitattributes").write_text(attributes, encoding="ascii")
    _run_git(repository, "add", ".gitattributes")
    _run_git(repository, "commit", "-m", "add attributes")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.autocrlf", "true"),
        ("core.eol", "crlf"),
        ("filter.optional.clean", "cat"),
        ("core.attributesFile", "custom-attributes"),
        ("core.excludesFile", "custom-excludes"),
        ("include.path", "included-config"),
    ],
)
def test_private_index_declines_conversion_or_include_config(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    if value in {"custom-attributes", "custom-excludes", "included-config"}:
        target = tmp_path / value
        target.write_text("", encoding="ascii")
        value = str(target)
    _run_git(repository, "config", key, value)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


def _repository_info_attributes_path(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The repository's own `.git/info/attributes`, with its parent created."""
    attributes = repository / ".git/info/attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    return attributes


def _global_attributes_path(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A global attributes file Git will read through `XDG_CONFIG_HOME`."""
    return _private_xdg_git_path(tmp_path, monkeypatch, "attributes")


def _system_attributes_path(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A system attributes file, installed by pointing the product's list at it."""
    attributes = tmp_path / "system-gitattributes"
    monkeypatch.setattr(
        workspace_revision,
        "SYSTEM_GIT_ATTRIBUTE_PATHS",
        (attributes,),
        raising=False,
    )
    return attributes


# One setup per parametrize id, so the test itself needs no branching. Each returns
# the attributes file to write an active rule into.
_ACTIVE_ATTRIBUTE_SOURCES = {
    "global": _global_attributes_path,
    "info": _repository_info_attributes_path,
    "system": _system_attributes_path,
}


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("source", sorted(_ACTIVE_ATTRIBUTE_SOURCES))
def test_private_index_declines_repository_global_and_system_attributes(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    attributes = _ACTIVE_ATTRIBUTE_SOURCES[source](repository, tmp_path, monkeypatch)
    attributes.write_text("*.py text\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert (result, exact_calls, private_calls) == (True, 1, 0)


def test_the_fixture_takes_this_machines_git_configuration_out_of_the_way() -> None:
    """The pinning the optimization assertions rest on, checked rather than assumed.

    Every `private_calls == 1` in this file is a claim about the product, and it
    only holds while the host cannot reach the fence. A runner's `/etc/gitconfig`
    carries a git-lfs clean/smudge filter, so an unpinned system path turned all
    of those assertions into assertions about `/etc`.
    """
    installation = workspace_revision.private_git_installation()
    if installation is None:
        # An unknown git layout: the product reads no system configuration there, so there
        # is nothing to pin and nothing to claim. See
        # `docs/research/2026-09-18-the-pinning-test-asks-only-where-there-is-something-to-pin.md`.
        pytest.skip("this machine's git layout offers no private installation")

    pinned = (*installation.system_config_paths, *installation.system_attribute_paths)

    assert (
        [path.exists() for path in pinned],
        [name in os.environ for name in workspace_revision.PRIVATE_GIT_SELECTOR_ENVIRONMENT],
        _indexed_git_config_variables(),
    ) == ([False] * len(pinned), [False] * 9, [])


# The `[filter "lfs"]` section `git lfs install --skip-repo --system` writes, which
# the git-lfs Debian package's postinst runs and every hosted Linux runner therefore
# carries in /etc/gitconfig. A clean/smudge filter changes what git reads of a working
# tree, so the fast path must stay off wherever it is installed.
LFS_SYSTEM_CONFIG = (
    '[filter "lfs"]\n'
    "\tclean = git-lfs clean -- %f\n"
    "\tsmudge = git-lfs smudge -- %f\n"
    "\tprocess = git-lfs filter-process\n"
    "\trequired = true\n"
)


_DECLINED_CONFIGS = {
    "global": "[core]\n\tautocrlf = false\n",
    "system": "[core]\n\tautocrlf = false\n",
    "system-git-lfs": LFS_SYSTEM_CONFIG,
}


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("source", sorted(_DECLINED_CONFIGS))
def test_private_index_declines_global_and_system_config_files(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    if source == "global":
        config_home = tmp_path / "xdg"
        config = config_home / "git/config"
        config.parent.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    else:
        config = tmp_path / "system-gitconfig"
        monkeypatch.setattr(
            workspace_revision,
            "SYSTEM_GIT_CONFIG_PATHS",
            (config,),
            raising=False,
        )
    config.write_text(_DECLINED_CONFIGS[source], encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert (result, exact_calls, private_calls) == (True, 1, 0)


def _commit_inert_attribute_file(repository: Path, name: str) -> None:
    """Commit a nested attributes-shaped file under `docs/` that declares no rule."""
    nested = repository / "docs"
    nested.mkdir()
    (nested / name).write_text("# no active rules\n", encoding="ascii")
    _run_git(repository, "add", f"docs/{name}")
    _run_git(repository, "commit", "-m", "add inert metadata name")


def _write_global_git_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, content: str
) -> None:
    """Write `git/<name>` under a private `XDG_CONFIG_HOME` and point Git at it."""
    _private_xdg_git_path(tmp_path, monkeypatch, name).write_text(content, encoding="ascii")


def _write_system_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    """Install a system-level Git configuration by pointing the product's list at it.

    Bytes rather than text so a case can name the exact file a machine ships.
    """
    config = tmp_path / "system-gitconfig"
    config.write_bytes(content)
    monkeypatch.setattr(
        workspace_revision,
        "SYSTEM_GIT_CONFIG_PATHS",
        (config,),
        raising=False,
    )


# One setup per parametrize id. Each installs an attribute or configuration file that
# Git reads but whose content changes no ignore, attribute or index semantics, so the
# private-index fast path must still be taken.
_IRRELEVANT_SEMANTICS_SOURCES = {
    "nested-empty-attributes": lambda repository, tmp_path, monkeypatch: (
        _commit_inert_attribute_file(repository, ".gitattributes")
    ),
    "irrelevant-attribute-name": lambda repository, tmp_path, monkeypatch: (
        _commit_inert_attribute_file(repository, "rules.gitattributes")
    ),
    "empty-global-attributes": lambda repository, tmp_path, monkeypatch: (
        _write_global_git_file(tmp_path, monkeypatch, "attributes", "# no active rules\n")
    ),
    "global-user-config": lambda repository, tmp_path, monkeypatch: (
        _write_global_git_file(
            tmp_path, monkeypatch, "config", "[user]\n\tname = Irrelevant Identity\n"
        )
    ),
    "system-user-config": lambda repository, tmp_path, monkeypatch: (
        _write_system_git_config(
            tmp_path, monkeypatch, b"[user]\n\temail = irrelevant@example.invalid\n"
        )
    ),
    # Exactly the /etc/gitconfig every GitHub-hosted Linux runner ships:
    # actions/runner-images appends these two lines in install-git.sh. safe.directory
    # says only where Git agrees to work, never what it reads of the working tree.
    "system-safe-directory": lambda repository, tmp_path, monkeypatch: (
        _write_system_git_config(tmp_path, monkeypatch, b"[safe]\n        directory = *\n")
    ),
}


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("case", sorted(_IRRELEVANT_SEMANTICS_SOURCES))
def test_private_index_accepts_semantically_irrelevant_attribute_and_config_files(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _IRRELEVANT_SEMANTICS_SOURCES[case](repository, tmp_path, monkeypatch)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert (result, exact_calls, private_calls) == (True, 0, 1)


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux Git ignore semantics")
def test_private_index_preserves_default_global_ignore_semantics(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "xdg"
    global_ignore = config_home / "git/ignore"
    global_ignore.parent.mkdir(parents=True)
    global_ignore.write_text("hidden.py\n", encoding="ascii")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    (repository / "hidden.py").write_text("hidden = True\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 0
    assert private_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_still_rejects_active_nested_attributes(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = repository / "pkg/.gitattributes"
    nested.write_text("*.py text eol=crlf\n", encoding="ascii")
    _run_git(repository, "add", "pkg/.gitattributes")
    _run_git(repository, "commit", "-m", "add nested attributes")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    ("name", "value"),
    [("GIT_ATTR_SOURCE", "HEAD"), ("GIT_CONFIG_COUNT", "0")],
)
def test_private_index_declines_attribute_or_config_selector_environment(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


def _qualify_private_index_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the platform as supported, so it is not the uncertainty under test."""
    monkeypatch.setattr(workspace_revision, "private_index_platform_supported", lambda: True)


def _uncertain_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform itself does not support the private index."""
    monkeypatch.setattr(workspace_revision, "private_index_platform_supported", lambda: False)


def _uncertain_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repository layout gives no ordinary index path to copy."""
    _qualify_private_index_platform(monkeypatch)
    monkeypatch.setattr(workspace_revision, "ordinary_index_path", lambda _root: None)


def _uncertain_candidate_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """The qualified index path names a file that is no longer there."""
    _qualify_private_index_platform(monkeypatch)
    monkeypatch.setattr(
        workspace_revision,
        "ordinary_index_path",
        lambda root: root / ".git/index-removed-after-qualification",
    )


def _uncertain_oversized(monkeypatch: pytest.MonkeyPatch) -> None:
    """The index is larger than the private copy is allowed to be."""
    _qualify_private_index_platform(monkeypatch)
    monkeypatch.setattr(workspace_revision, "MAX_PRIVATE_INDEX_BYTES", 1)


# One setup per parametrize id: each leaves the private-index path unable to prove
# itself for a different reason, and every reason must end on the exact fallback.
_PRIVATE_INDEX_UNCERTAINTIES = {
    "candidate-race": _uncertain_candidate_race,
    "layout": _uncertain_layout,
    "oversized": _uncertain_oversized,
    "platform": _uncertain_platform,
}


@pytest.mark.parametrize("uncertainty", sorted(_PRIVATE_INDEX_UNCERTAINTIES))
def test_private_index_uncertainty_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    uncertainty: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    _PRIVATE_INDEX_UNCERTAINTIES[uncertainty](monkeypatch)

    exact_calls = 0
    real_exact = workspace_revision.git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private status used")),
    )

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux index variants")
@pytest.mark.parametrize("variant", ["v4", "split"])
def test_private_index_git_variants_use_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    if variant == "v4":
        _run_git(repository, "update-index", "--index-version", "4")
    else:
        _run_git(repository, "update-index", "--split-index")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    real_exact = workspace_revision.git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private status used")),
    )

    workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux worktree layouts")
@pytest.mark.parametrize("layout", ["linked-worktree", "symlink-index"])
def test_private_index_nonordinary_layout_uses_exact_fallback(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    if layout == "linked-worktree":
        root = tmp_path / "linked"
        _run_git(repository, "worktree", "add", "--detach", str(root), "HEAD")
    else:
        root = repository
        index = root / ".git/index"
        target = root / ".git/index-owned"
        index.rename(target)
        index.symlink_to(target.name)
    scope = resolve_repository_scope(root)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    real_exact = workspace_revision.git_state

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private status used")),
    )

    workspace_revision.verify_workspace_revision_unchanged(scope, expected)

    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux Git installation semantics")
def test_private_index_custom_git_prefix_uses_exact_fallback(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("Git executable is unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    custom_bin = tmp_path / "custom-prefix/bin"
    custom_bin.mkdir(parents=True)
    wrapper = custom_bin / "git"
    wrapper.write_text(
        "#!/bin/sh\n" 'exec "$REVISION_REAL_GIT" "$@"\n',
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("REVISION_REAL_GIT", real_git)
    monkeypatch.setenv("PATH", f"{custom_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_rejects_symlinked_reference_parent_before_ref_mutation(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_head = _run_git(repository, "rev-parse", "HEAD").decode("ascii").strip()
    _run_git(repository, "commit", "--allow-empty", "-m", "second head")
    branch = _run_git(repository, "symbolic-ref", "--short", "HEAD").decode("ascii").strip()
    heads = repository / ".git/refs/heads"
    external_heads = tmp_path / "external-heads"
    heads.rename(external_heads)
    try:
        heads.symlink_to(external_heads, target_is_directory=True)
    except OSError:
        external_heads.rename(heads)
        pytest.skip("directory symlinks are unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    exact_calls = 0
    private_calls = 0
    real_exact = workspace_revision.git_state
    real_private = workspace_revision.git_state_with_private_index

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    def recording_private(*args, **kwargs):
        nonlocal private_calls
        private_calls += 1
        return real_private(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
    monkeypatch.setattr(workspace_revision, "git_state_with_private_index", recording_private)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    (external_heads / branch).write_text(f"{first_head}\n", encoding="ascii")
    assert (
        workspace_revision.verify_workspace_revision_unchanged(scope, expected),
        exact_calls,
        private_calls,
    ) == (False, 2, 0)


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_rejects_symlinked_info_parent_even_without_attributes(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = repository / ".git/info"
    external_info = tmp_path / "external-info"
    external_info.mkdir()
    try:
        info.symlink_to(external_info, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0
