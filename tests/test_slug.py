"""Regression tests: slug computation, collision resolution, strict ownership.

Covers the Round 2 / Round 5 fixes to `session_start_project_state.py`:
  - Base slug sanitization (Cyrillic preservation, hyphens, edge cases).
  - Collision resolution: base → parent-of-parent → git owner-repo → grandparent → path-hash.
  - Strict ownership: state.md without a `- Project root:` line is NOT owned.
  - Idempotency: re-compute returns the same slug.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

import bootstrap_project
import memory_state
import pytest
import session_end_project_tag
import session_start_context
import session_start_project_state
from session_start_project_state import (
    _base_slug,
    _compute_slug,
    _git_remote_slug,
    _path_hash_suffix,
    _render_new_state,
    _slug_owns_dir,
)

_UNICODE_NONCHARACTERS = tuple(chr(codepoint) for codepoint in range(0xFDD0, 0xFDF0)) + tuple(
    chr((plane << 16) | suffix)
    for plane in range(17)
    for suffix in (0xFFFE, 0xFFFF)
)


def _synchronize_project_claim_lock(monkeypatch) -> list[Path]:
    current_memory_state = sys.modules.get("memory_state", memory_state)
    real_lock = current_memory_state.advisory_file_lock
    contenders_ready = threading.Barrier(2)
    calls: list[Path] = []

    @contextmanager
    def synchronized_lock(path, *args, **kwargs):
        lock_path = Path(path)
        if lock_path.name == "project-state-claim.lock":
            calls.append(lock_path)
            contenders_ready.wait(timeout=5)
        with real_lock(path, *args, **kwargs):
            yield

    monkeypatch.setattr(current_memory_state, "advisory_file_lock", synchronized_lock)
    return calls


# ---------- _base_slug ----------

def test_base_slug_lowercase():
    p = Path("/tmp/My-Project")
    assert _base_slug(p) == "my-project"


def test_base_slug_preserves_cyrillic():
    p = Path("/tmp/Тесты")
    assert _base_slug(p) == "тесты"


def test_base_slug_strips_unsafe_chars():
    # Any Path with weird basename characters
    class P:
        name = "foo:bar*baz"
    assert _base_slug(P()) == "foo-bar-baz"  # type: ignore[arg-type]


def test_base_slug_fallback_for_empty_and_dotdot():
    class P:
        name = ""
    assert _base_slug(P()) == "root"  # type: ignore[arg-type]
    P.name = ".."
    assert _base_slug(P()) == "root"  # type: ignore[arg-type]


def test_slug_grammar_replaces_every_record_and_markdown_delimiter():
    delimiters = (
        "`", "|", ":", "_", "/", "\\", "#", "[", "]", "(", ")",
        "{", "}", "<", ">", "*", "!", "+", "=", "~", "^", "&", "%",
        "?", '"', "'", " ", "\t", "\n", "\x00",
    )

    for delimiter in delimiters:
        allocated = session_start_project_state._sanitize(
            f"alpha{delimiter}beta"
        )
        assert allocated == "alpha-beta", repr(delimiter)
        assert session_start_context._normalize_project_slug(allocated) == allocated
        assert session_start_context._normalize_project_slug(
            f"alpha{delimiter}beta"
        ) is None

    assert session_start_project_state._sanitize("Тест.42-SAFE") == "тест.42-safe"
    assert session_start_context._normalize_project_slug("тест.42-safe") == (
        "тест.42-safe"
    )


def test_delimiter_heavy_project_claim_is_stable_and_prompt_selectable(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n"
        "- Project root JSON: <absolute path JSON>\n"
        "- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    project = tmp_path / "Alpha`[Beta]#(Gamma)_Delta!"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    first = session_start_project_state.confirm_project_identity(project, projects)
    second = session_start_project_state.confirm_project_identity(project, projects)

    assert first is not None and second is not None
    assert first[0] == second[0] == "alpha-beta-gamma-delta"
    assert first[1] == second[1]
    assert (first[2], second[2]) == (True, False)
    state_body = first[1].read_text(encoding="utf-8")
    [runtime_slug_line] = [
        line
        for line in state_body.splitlines()
        if line.startswith("- Runtime slug JSON:")
    ]
    assert json.loads(runtime_slug_line.split(":", 1)[1].strip()) == first[0]
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        f"- `[10:00:00] prompt | ses_1234 | {first[0]}` "
        f"project-root-json={json.dumps(str(project.resolve()))} | "
        "CANONICAL_SLUG_PROMPT\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, first[0], project.resolve())

    assert [record.slug for record in records] == [first[0]]
    assert "CANONICAL_SLUG_PROMPT" in excerpt


def test_legacy_noncanonical_slug_ownership_uses_safe_runtime_identity(tmp_path: Path):
    projects = tmp_path / "projects"
    project = tmp_path / "active"
    project.mkdir()
    state_path = projects / "legacy_slug`folder" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n",
        encoding="utf-8",
    )

    assert _slug_owns_dir(state_path.parent.name, project, projects) is True
    assert session_end_project_tag._lookup_existing_slug(project, projects) == "active"


def test_owned_legacy_state_is_reused_for_identity_but_not_bootstrap_context(
    monkeypatch,
    tmp_path: Path,
):
    import bootstrap_project
    import codex_memory

    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n"
        "## Where we left off\n- new template must not be used\n"
        "- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    project = tmp_path / "workspace-a" / "svc-name"
    (project / ".git").mkdir(parents=True)
    legacy_state = projects / "svc`name" / "state.md"
    legacy_state.parent.mkdir(parents=True)
    legacy_body = (
        "# Legacy service\n\n"
        "## Where we left off\n- LEGACY_HANDOFF_SENTINEL\n\n"
        f"- Project root: `{project.resolve()}`\n"
    )
    legacy_state.write_text(legacy_body, encoding="utf-8")
    other_project = tmp_path / "workspace-b" / "svc-name"
    other_project.mkdir(parents=True)
    occupied = projects / "svc-name" / "state.md"
    occupied.parent.mkdir(parents=True)
    occupied.write_text(
        f"- Project root JSON: {json.dumps(str(other_project.resolve()))}\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(runtime))

    first = session_start_project_state.confirm_project_identity(project, projects)
    second = session_start_project_state.confirm_project_identity(project, projects)
    resolved = session_start_project_state.resolve_project_state(project, projects)

    assert first is not None and second is not None
    expected_slug = "svc-name-workspace-a"
    assert first == (expected_slug, legacy_state.resolve(), False)
    assert second == first
    assert resolved == (expected_slug, legacy_state.resolve())
    assert session_start_project_state.is_canonical_project_slug(expected_slug)
    backfilled = legacy_state.read_text(encoding="utf-8")
    assert backfilled.split("- Project root:", 1)[0] == legacy_body.split(
        "- Project root:", 1
    )[0]
    assert backfilled.count("LEGACY_HANDOFF_SENTINEL") == 1
    [runtime_slug_line] = [
        line
        for line in backfilled.splitlines()
        if line.startswith("- Runtime slug JSON:")
    ]
    assert json.loads(runtime_slug_line.split(":", 1)[1].strip()) == expected_slug

    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)
    monkeypatch.setattr(
        session_start_project_state,
        "_bootstrap_project_state",
        lambda *_args: None,
    )
    assert session_start_context._resolve_project(project) == resolved

    monkeypatch.setattr(codex_memory, "PROJECTS_DIR", projects)
    assert codex_memory._state_path(project.resolve()) == resolved
    assert session_end_project_tag._compute_slug(project, projects) == expected_slug

    monkeypatch.setattr(bootstrap_project, "ROOT", vault)
    monkeypatch.setattr(bootstrap_project, "PROJECTS_DIR", projects)
    monkeypatch.setattr(bootstrap_project, "STATE_DIR", runtime / "run")
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd, **_kwargs: [])
    monkeypatch.setattr(
        bootstrap_project,
        "_extract_readme_summary",
        lambda _cwd: "LEGACY_BOOTSTRAP_SENTINEL",
    )
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_docs_structure", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args, **_kwargs: "")
    result = bootstrap_project.bootstrap(str(project), apply=True)

    assert result.startswith("Skipped:")
    assert not (legacy_state.parent / "bootstrap.md").exists()
    assert not (projects / expected_slug).exists()
    owned_states = [
        path.resolve()
        for path in projects.glob("*/state.md")
        if session_start_project_state._recorded_project_root(
            path.read_text(encoding="utf-8")
        )
        == str(project.resolve())
    ]
    assert owned_states == [legacy_state.resolve()]

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        f"- `[10:00:00] prompt | ses_legacy | {expected_slug}` "
        f"project-root-json={json.dumps(str(project.resolve()))} | "
        "LEGACY_PROMPT_SENTINEL\n",
        encoding="utf-8",
    )
    excerpt = session_start_context.daily_excerpt(
        daily,
        expected_slug,
        project.resolve(),
    )
    assert "LEGACY_PROMPT_SENTINEL" in excerpt


def test_legacy_ownership_scan_is_complete_and_keeps_reads_bounded(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    project = tmp_path / "target"
    project.mkdir()
    malformed = projects / "00-malformed" / "state.md"
    malformed.parent.mkdir(parents=True)
    malformed.write_text(
        '- Project root JSON: "unterminated\n',
        encoding="utf-8",
    )
    legacy_state = projects / "legacy`target" / "state.md"
    legacy_state.parent.mkdir(parents=True)
    legacy_state.write_text(
        f"- Project root: `{project.resolve()}`\n",
        encoding="utf-8",
    )
    entries = [malformed.parent, legacy_state.parent]
    entries.extend(projects / f"decoy-{index:04d}" for index in range(600))

    real_iterdir = Path.iterdir
    yielded = 0

    def guarded_iterdir(path):
        nonlocal yielded
        if path != projects:
            yield from real_iterdir(path)
            return
        for entry in entries:
            yielded += 1
            yield entry

    real_fdopen = os.fdopen
    read_sizes: list[int] = []

    class TrackingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    def tracking_fdopen(*args, **kwargs):
        return TrackingFile(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    monkeypatch.setattr(os, "fdopen", tracking_fdopen)

    slug, state_path = session_start_project_state.resolve_project_state(
        project,
        projects,
    )

    assert slug == "target"
    assert state_path == legacy_state.resolve()
    assert state_path.is_relative_to(projects.resolve())
    assert yielded == len(entries)
    assert read_sizes
    assert all(0 <= size <= 65_537 for size in read_sizes)


def test_state_inventory_lstats_entries_before_any_resolution(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    project = tmp_path / "project"
    project.mkdir()
    state_path = projects / "active" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "active"\n',
        encoding="utf-8",
    )
    seen_lstat: set[Path] = set()
    real_lstat = Path.lstat
    real_resolve = Path.resolve

    def tracking_lstat(path: Path, *args, **kwargs):
        seen_lstat.add(Path(os.path.abspath(path)))
        return real_lstat(path, *args, **kwargs)

    def guarded_resolve(path: Path, *args, **kwargs):
        absolute = Path(os.path.abspath(path))
        if absolute in {state_path, state_path.parent} and absolute not in seen_lstat:
            raise AssertionError("state inventory resolved an entry before lstat")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", tracking_lstat)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    [entry] = session_start_project_state._scan_project_states(projects)

    assert entry.state_path == state_path
    assert state_path in seen_lstat


def test_unowned_resolution_uses_one_complete_project_scan(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    projects.mkdir()
    project = tmp_path / "target"
    project.mkdir()
    entries = [projects / f"decoy-{index:04d}" for index in range(600)]
    yielded = 0

    def guarded_iterdir(path):
        nonlocal yielded
        assert path == projects
        for entry in entries:
            yielded += 1
            yield entry

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    slug, state_path = session_start_project_state.resolve_project_state(
        project,
        projects,
    )

    assert slug == "target"
    assert state_path == (projects / "target" / "state.md").resolve()
    assert yielded == len(entries)


def test_confirm_project_identity_returns_only_owned_or_atomically_claimed_state(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    marked = tmp_path / "marked"
    (marked / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert (
        session_start_project_state.confirm_project_identity(unmarked, projects)
        is None
    )
    first = session_start_project_state.confirm_project_identity(marked, projects)
    second = session_start_project_state.confirm_project_identity(marked, projects)

    assert first is not None
    assert second == (first[0], first[1], False)
    assert first[2] is True
    assert first[1].is_file()
    assert session_start_project_state._state_path_owns_project(
        first[1], marked.resolve()
    )


@pytest.mark.parametrize(
    "template_body",
    (
        (
            "# <Project Name>\n"
            "- Project root JSON: <absolute path JSON>\n"
            "- Project root JSON: <absolute path JSON>\n"
        ),
        '# <Project Name>\n- Project root JSON: "D:/different-project"\n',
    ),
)
def test_malformed_rendered_template_never_publishes_project_claim(
    monkeypatch,
    tmp_path: Path,
    template_body: str,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(template_body, encoding="utf-8")
    project = tmp_path / "workspace" / "service"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert session_start_project_state.confirm_project_identity(project, projects) is None
    assert not list(
        path for path in projects.glob("*/state.md") if path.parent.name != "_template"
    )


def test_nonmetadata_template_key_prefix_prose_is_preserved_and_claimed(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    prose = "- Project root JSON migration notes remain ordinary project content."
    template.write_text(f"# <Project Name>\n{prose}\n", encoding="utf-8")
    project = tmp_path / "workspace" / "service"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    claimed = session_start_project_state.confirm_project_identity(project, projects)

    assert claimed is not None
    assert prose in claimed[1].read_text(encoding="utf-8")


def test_complete_registry_reserves_alias_beyond_entry_512(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    other = tmp_path / "other"
    other.mkdir()
    for index in range(520):
        state_path = projects / f"entry-{index:04d}" / "state.md"
        state_path.parent.mkdir(parents=True)
        alias = "shared" if index == 519 else f"reserved-{index:04d}"
        state_path.write_text(
            f"- Project root JSON: {json.dumps(str(other.resolve()))}\n"
            f"- Runtime slug JSON: {json.dumps(alias)}\n",
            encoding="utf-8",
        )
    project = tmp_path / "workspace-b" / "shared"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    claimed = session_start_project_state.confirm_project_identity(project, projects)

    assert claimed is not None
    assert claimed[0] != "shared"
    assert claimed[1] != (projects / "shared" / "state.md").resolve()
    assert not (projects / "shared" / "state.md").exists()


def test_opaque_underscore_state_directory_reserves_its_canonical_alias(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    opaque = projects / "_shared" / "state.md"
    opaque.parent.mkdir()
    opaque.write_text("# Opaque state with missing ownership\n", encoding="utf-8")
    project = tmp_path / "workspace" / "shared"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    claimed = session_start_project_state.confirm_project_identity(project, projects)

    assert claimed is not None
    assert claimed[0] != "shared"
    assert claimed[1] != (projects / "shared" / "state.md").resolve()
    assert opaque.read_text(encoding="utf-8").startswith("# Opaque state")


def test_opaque_state_reserves_parseable_runtime_alias(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    opaque = projects / "opaque-folder" / "state.md"
    opaque.parent.mkdir()
    opaque.write_text(
        '- Project root JSON: "unterminated\n'
        '- Runtime slug JSON: "shared"\n',
        encoding="utf-8",
    )
    project = tmp_path / "workspace" / "shared"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    claimed = session_start_project_state.confirm_project_identity(project, projects)

    assert claimed is not None
    assert claimed[0] != "shared"
    assert json.loads(
        next(
            line.split(":", 1)[1]
            for line in opaque.read_text(encoding="utf-8").splitlines()
            if line.startswith("- Runtime slug JSON:")
        )
    ) == "shared"


def test_registry_scan_error_fails_closed_without_allocating(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    project = tmp_path / "workspace" / "service"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    def interrupted_scan(_path):
        yield template.parent
        raise OSError("injected registry failure")

    monkeypatch.setattr(Path, "iterdir", interrupted_scan)

    assert session_start_project_state.confirm_project_identity(project, projects) is None
    assert not (projects / "service" / "state.md").exists()


def test_invalid_utf8_state_fails_closed_without_rewriting_original_bytes(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    project = tmp_path / "workspace" / "service"
    project.mkdir(parents=True)
    state_path = projects / "legacy-service" / "state.md"
    state_path.parent.mkdir(parents=True)
    original = (
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n".encode()
        + b"## Where we left off\n- invalid byte: \xff\n"
    )
    state_path.write_bytes(original)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert session_start_project_state.confirm_project_identity(project, projects) is None
    assert state_path.read_bytes() == original
    assert b"Runtime slug JSON" not in state_path.read_bytes()


@pytest.mark.parametrize(
    "runtime_metadata",
    (
        '- Runtime slug JSON: "unterminated\n',
        '- Runtime slug JSON: "service"\n- Runtime slug JSON: "duplicate"\n',
        '- Runtime slug JSON: "unsafe_alias"\n',
    ),
)
def test_present_invalid_runtime_slug_invalidates_registry_without_backfill(
    monkeypatch,
    tmp_path: Path,
    runtime_metadata: str,
):
    projects = tmp_path / "projects"
    project = tmp_path / "workspace" / "service"
    project.mkdir(parents=True)
    state_path = projects / "service" / "state.md"
    state_path.parent.mkdir(parents=True)
    original = (
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        f"{runtime_metadata}"
    )
    state_path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert session_start_project_state.confirm_project_identity(project, projects) is None
    assert state_path.read_text(encoding="utf-8") == original


def test_registry_entry_overflow_fails_closed_without_reusing_or_mutating(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    project = tmp_path / "workspace" / "service"
    project.mkdir(parents=True)
    state_path = projects / "owned" / "state.md"
    state_path.parent.mkdir(parents=True)
    original = f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
    state_path.write_text(original, encoding="utf-8")
    cap = getattr(session_start_project_state, "MAX_PROJECT_STATE_ENTRIES", 1024)
    entries = [state_path.parent]
    entries.extend(projects / f"missing-{index:05d}" for index in range(cap))
    yielded = 0

    def overflowing_iterdir(path):
        nonlocal yielded
        assert path == projects
        for entry in entries:
            yielded += 1
            yield entry

    monkeypatch.setattr(Path, "iterdir", overflowing_iterdir)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert session_start_project_state.confirm_project_identity(project, projects) is None
    assert yielded == cap + 1
    assert state_path.read_text(encoding="utf-8") == original


def test_registry_entry_overflow_fails_closed_without_allocating(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    project = tmp_path / "workspace" / "service"
    (project / ".git").mkdir(parents=True)
    cap = getattr(session_start_project_state, "MAX_PROJECT_STATE_ENTRIES", 1024)
    entries = [template.parent]
    entries.extend(projects / f"missing-{index:05d}" for index in range(cap))
    yielded = 0

    def overflowing_iterdir(path):
        nonlocal yielded
        assert path == projects
        for entry in entries:
            yielded += 1
            yield entry

    monkeypatch.setattr(Path, "iterdir", overflowing_iterdir)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert session_start_project_state.confirm_project_identity(project, projects) is None
    assert yielded == cap + 1
    assert not (projects / "service").exists()


def test_duplicate_exact_root_claims_fail_closed_without_mutating_either_state(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    project = tmp_path / "workspace" / "service"
    project.mkdir(parents=True)
    state_paths = [
        projects / "first" / "state.md",
        projects / "second" / "state.md",
    ]
    original_bodies = []
    for index, state_path in enumerate(state_paths):
        state_path.parent.mkdir(parents=True)
        body = (
            f"# Duplicate owner {index}\n"
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
            f"- PRESERVE_{index}\n"
        )
        state_path.write_text(body, encoding="utf-8")
        original_bodies.append(body)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert session_start_project_state.confirm_project_identity(project, projects) is None
    assert [
        state_path.read_text(encoding="utf-8") for state_path in state_paths
    ] == original_bodies


def test_legacy_mixed_case_alias_normalizes_in_place_and_remains_retrievable(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    project = tmp_path / "workspace" / "service"
    project.mkdir(parents=True)
    state_path = projects / "legacy-folder" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Legacy service\n"
        "## Where we left off\n- PRESERVED_HANDOFF\n"
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "Service"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    confirmed = session_start_project_state.confirm_project_identity(project, projects)

    assert confirmed == ("service", state_path.resolve(), False)
    migrated = state_path.read_text(encoding="utf-8")
    assert migrated.count("PRESERVED_HANDOFF") == 1
    assert session_start_project_state._recorded_runtime_slug(migrated) == "service"

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "- `[10:00:00] prompt | session | Service` "
        f"project-root-json={json.dumps(str(project.resolve()))} | "
        "PRE_MIGRATION_ALIAS_PROMPT\n",
        encoding="utf-8",
    )
    excerpt = session_start_context.daily_excerpt(
        daily,
        confirmed[0],
        project.resolve(),
    )
    assert "PRE_MIGRATION_ALIAS_PROMPT" in excerpt


def test_mixed_case_alias_reservation_blocks_case_equivalent_allocation(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    first = tmp_path / "workspace-a" / "service"
    second = tmp_path / "workspace-b" / "service"
    first.mkdir(parents=True)
    (second / ".git").mkdir(parents=True)
    first_state = projects / "legacy-first" / "state.md"
    first_state.parent.mkdir()
    first_state.write_text(
        f"- Project root JSON: {json.dumps(str(first.resolve()))}\n"
        '- Runtime slug JSON: "Service"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    second_claim = session_start_project_state.confirm_project_identity(second, projects)
    first_claim = session_start_project_state.confirm_project_identity(first, projects)

    assert second_claim is not None and first_claim is not None
    assert second_claim[0] == "service-workspace-b"
    assert first_claim == ("service", first_state.resolve(), False)
    assert second_claim[0].casefold() != first_claim[0].casefold()


def test_casefold_duplicate_persisted_aliases_fail_closed_without_migration(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    roots = [
        tmp_path / "workspace-a" / "service",
        tmp_path / "workspace-b" / "service",
    ]
    aliases = ("Service", "service")
    state_paths = []
    original_bodies = []
    for index, (project, alias) in enumerate(zip(roots, aliases, strict=True)):
        project.mkdir(parents=True)
        state_path = projects / f"legacy-{index}" / "state.md"
        state_path.parent.mkdir(parents=True)
        body = (
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
            f"- Runtime slug JSON: {json.dumps(alias)}\n"
        )
        state_path.write_text(body, encoding="utf-8")
        state_paths.append(state_path)
        original_bodies.append(body)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    assert session_start_project_state.confirm_project_identity(roots[0], projects) is None
    assert [path.read_text(encoding="utf-8") for path in state_paths] == original_bodies


def test_runtime_alias_is_independent_of_another_projects_physical_folder(
    monkeypatch,
    tmp_path: Path,
):
    import build_context

    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    first = tmp_path / "workspace-a" / "service"
    second = tmp_path / "workspace-b" / "service"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    first_state = projects / "legacy-first" / "state.md"
    first_state.parent.mkdir(parents=True)
    first_state.write_text(
        f"- Project root JSON: {json.dumps(str(first.resolve()))}\n"
        '- Runtime slug JSON: "service"\n'
        "## Where we left off\n- FIRST_PROJECT_HANDOFF\n",
        encoding="utf-8",
    )
    second_state = projects / "service" / "state.md"
    second_state.parent.mkdir()
    second_state.write_text(
        f"- Project root JSON: {json.dumps(str(second.resolve()))}\n"
        '- Runtime slug JSON: "other-service"\n',
        encoding="utf-8",
    )
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "service-history.md").write_text(
        "---\n"
        "type: pattern\n"
        "project: service\n"
        f"project_root: {json.dumps(str(first.resolve()))}\n"
        "---\n"
        "# Service history\n"
        "One-sentence summary: LEGACY_FRONTMATTER_SCOPE\n",
        encoding="utf-8",
    )
    daily_dir = vault / "knowledge" / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text(
        "- `[10:00:00] prompt | session | service` "
        f"project-root-json={json.dumps(str(first.resolve()))} | "
        "LEGACY_DAILY_SCOPE\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(build_context, "ROOT", vault)
    monkeypatch.setattr(build_context, "KNOWLEDGE", notes)
    monkeypatch.setattr(build_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(build_context, "PROJECTS_DIR", projects)
    monkeypatch.setattr(build_context, "load_state", lambda: {})

    confirmed = session_start_project_state.confirm_project_identity(first, projects)

    assert confirmed == ("service", first_state.resolve(), False)
    context = build_context.build_context("service")
    assert "LEGACY_FRONTMATTER_SCOPE" in context
    assert "LEGACY_DAILY_SCOPE" in context
    assert "FIRST_PROJECT_HANDOFF" in context


def test_persisted_legacy_alias_prevents_later_takeover_and_daily_leakage(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "vault" / "knowledge" / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n"
        "## Where we left off\n- new state\n"
        "- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    project_a = tmp_path / "workspace-a" / "svc-name"
    project_b = tmp_path / "workspace-b" / "svc-name"
    for project in (project_a, project_b):
        (project / ".git").mkdir(parents=True)
    legacy_state = projects / "svc`name" / "state.md"
    legacy_state.parent.mkdir(parents=True)
    legacy_state.write_text(
        "# Legacy A\n\n"
        "## Where we left off\n- PROJECT_A_HANDOFF\n\n"
        f"- Project root: `{project_a.resolve()}`\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    claim_a = session_start_project_state.confirm_project_identity(project_a, projects)
    claim_b = session_start_project_state.confirm_project_identity(project_b, projects)

    assert claim_a is not None and claim_b is not None
    alias_a, state_a, is_new_a = claim_a
    alias_b, state_b, is_new_b = claim_b
    assert (alias_a, state_a, is_new_a) == (
        "svc-name",
        legacy_state.resolve(),
        False,
    )
    assert alias_b != alias_a
    assert alias_b == "svc-name-workspace-b"
    assert state_b == (projects / alias_b / "state.md").resolve()
    assert is_new_b is True
    assert session_start_project_state.resolve_project_state(
        project_a, projects
    ) == (alias_a, legacy_state.resolve())
    assert session_start_project_state.resolve_project_state(
        project_b, projects
    ) == (alias_b, state_b)
    assert session_end_project_tag._compute_slug(project_a, projects) == alias_a
    assert session_end_project_tag._compute_slug(project_b, projects) == alias_b

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        f"- `[10:00:00] prompt | ses_b | {alias_b}` "
        f"project-root-json={json.dumps(str(project_b.resolve()))} | PROJECT_B_ONLY\n"
        f"- `[11:00:00] prompt | ses_a | {alias_a}` "
        f"project-root-json={json.dumps(str(project_a.resolve()))} | PROJECT_A_PRIVATE\n",
        encoding="utf-8",
    )
    excerpt_b = session_start_context.daily_excerpt(
        daily,
        alias_b,
        project_b.resolve(),
    )
    assert "PROJECT_B_ONLY" in excerpt_b
    assert "PROJECT_A_PRIVATE" not in excerpt_b


def test_session_end_unclaimed_project_has_no_base_slug_fallback(tmp_path: Path):
    projects = tmp_path / "projects"
    projects.mkdir()
    project = tmp_path / "Alpha`[Beta]#(Gamma)_Delta!"
    project.mkdir()

    assert session_end_project_tag._compute_slug(project, projects) is None


# ---------- _git_remote_slug ----------

def test_git_remote_slug_parses_ssh(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:Owner/Repo.git\n',
        encoding="utf-8",
    )
    assert _git_remote_slug(tmp_path) == "owner-repo"


def test_git_remote_slug_parses_https(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/Alice/my-app\n',
        encoding="utf-8",
    )
    assert _git_remote_slug(tmp_path) == "alice-my-app"


def test_git_remote_slug_none_without_git_or_origin(tmp_path: Path):
    assert _git_remote_slug(tmp_path) is None
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    assert _git_remote_slug(tmp_path) is None


def test_git_remote_slug_rejects_oversized_config_with_one_bounded_read(
    monkeypatch,
    tmp_path: Path,
):
    config = tmp_path / ".git" / "config"
    config.parent.mkdir()
    limit = getattr(session_start_project_state, "MAX_GIT_CONFIG_BYTES", 64 * 1024)
    config.write_bytes(
        b'[remote "origin"]\n\turl = git@example.test:owner/repository.git\n'
        + (b"x" * limit)
    )
    real_open = Path.open
    read_sizes: list[int] = []

    class TrackingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return TrackingFile(handle) if path == config else handle

    monkeypatch.setattr(Path, "open", tracking_open)

    assert _git_remote_slug(tmp_path) is None
    assert read_sizes == [limit + 1]


def test_git_remote_slug_rejects_non_utf8_config(tmp_path: Path):
    config = tmp_path / ".git" / "config"
    config.parent.mkdir()
    config.write_bytes(
        b'[remote "origin"]\n\turl = git@example.test:owner/repository.git\n\xff'
    )

    assert _git_remote_slug(tmp_path) is None


def test_free_base_slug_never_opens_git_config(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    projects.mkdir()
    project = tmp_path / "workspace" / "service"
    config = project / ".git" / "config"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[remote "origin"]\n\turl = git@example.test:owner/repository.git\n',
        encoding="utf-8",
    )
    real_open = Path.open
    touched: list[Path] = []

    def reject_config_open(path, *args, **kwargs):
        if path == config:
            touched.append(path)
            raise AssertionError("free basename must not inspect .git/config")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_config_open)

    assert _compute_slug(project, projects) == "service"
    assert touched == []


# ---------- _path_hash_suffix ----------

def test_path_hash_deterministic(tmp_path: Path):
    suffix = _path_hash_suffix(tmp_path)
    assert suffix == _path_hash_suffix(tmp_path)
    assert len(suffix) == 6


# ---------- _slug_owns_dir (strict ownership, Round 5 #3) ----------

def test_slug_owns_empty_dir(tmp_path: Path):
    """Unused slug → free to take."""
    projects = tmp_path / "projects"
    projects.mkdir()
    assert _slug_owns_dir("unused", Path("/any/project"), projects) is True


def test_slug_owns_matching_root(tmp_path: Path):
    projects = tmp_path / "projects"
    slug_dir = projects / "mine"
    slug_dir.mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    (slug_dir / "state.md").write_text(
        f"# mine — State\n- Project root: `{project}`\n", encoding="utf-8"
    )
    assert _slug_owns_dir("mine", project, projects) is True


def test_slug_owns_rejects_different_root(tmp_path: Path):
    projects = tmp_path / "projects"
    slug_dir = projects / "shared"
    slug_dir.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    mine = tmp_path / "mine"
    mine.mkdir()
    (slug_dir / "state.md").write_text(
        f"# shared — State\n- Project root: `{other}`\n", encoding="utf-8"
    )
    assert _slug_owns_dir("shared", mine, projects) is False


def test_slug_owns_strict_rejects_missing_source(tmp_path: Path):
    """Round 5 #3: state.md without `- Project root:` → NOT ours.

    Previously this returned True (assumed hand-edited, treat as ours),
    opening a collision hole where a second project could silently adopt
    a first project's state.md by having its Source section removed.
    """
    projects = tmp_path / "projects"
    slug_dir = projects / "ambiguous"
    slug_dir.mkdir(parents=True)
    (slug_dir / "state.md").write_text(
        "# ambiguous — State\n(no Source section whatsoever)\n",
        encoding="utf-8",
    )
    assert _slug_owns_dir("ambiguous", tmp_path / "someproj", projects) is False


def test_slug_owns_uses_canonical_json_over_conflicting_legacy_roots(tmp_path: Path):
    projects = tmp_path / "projects"
    state_path = projects / "mine" / "state.md"
    state_path.parent.mkdir(parents=True)
    project = tmp_path / "project-with-`-marker"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    state_path.write_text(
        "# mine\n"
        f"- Project root JSON: {json.dumps(str(project), ensure_ascii=False)}\n"
        f"- Project root: `{other}`\n",
        encoding="utf-8",
    )

    assert _slug_owns_dir("mine", project, projects) is True


def test_slug_owns_fails_closed_when_json_root_is_malformed(tmp_path: Path):
    projects = tmp_path / "projects"
    state_path = projects / "mine" / "state.md"
    state_path.parent.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    state_path.write_text(
        "# mine\n"
        "- Project root JSON: \"unterminated\n"
        f"- Project root: `{project}`\n",
        encoding="utf-8",
    )

    assert _slug_owns_dir("mine", project, projects) is False


@pytest.mark.parametrize(
    ("platform", "root", "expected"),
    (
        ("win32", r"C:\workspace\project", True),
        ("win32", r"\\server\share\project", True),
        ("win32", r"\workspace\project", False),
        ("win32", "/workspace/project", False),
        ("linux", "/workspace/project", True),
        ("darwin", "/workspace/project", True),
        ("linux", r"C:\workspace\project", False),
        ("linux", "../target", False),
        ("win32", "C:\\workspace\\project\0forged", False),
        ("linux", "/workspace/project\nforged", False),
        ("linux", "", False),
    ),
)
def test_project_root_requires_a_bounded_native_absolute_path(
    platform: str,
    root: str,
    expected: bool,
):
    assert (
        session_start_project_state._is_native_absolute_root(root, platform)
        is expected
    )
    assert (
        session_start_project_state._is_native_absolute_root(
            "x" * (session_start_project_state.MAX_PROJECT_ROOT_CHARS + 1),
            platform,
        )
        is False
    )


@pytest.mark.parametrize(
    "forbidden",
    (
        "\x00",
        "\x1f",
        "\x7f",
        "\x85",
        "\x9f",
        "\u2028",
        "\u2029",
        "\ud800",
        "\udfff",
        *_UNICODE_NONCHARACTERS,
    ),
    ids=lambda value: f"U+{ord(value):04X}",
)
@pytest.mark.parametrize(
    ("platform", "root"),
    (("win32", "C:/workspace/project"), ("linux", "/workspace/project")),
)
def test_project_root_rejects_unsafe_identity_characters(
    platform: str,
    root: str,
    forbidden: str,
):
    assert not session_start_project_state._is_native_absolute_root(
        f"{root}{forbidden}forged",
        platform,
    )


@pytest.mark.parametrize(
    "noncharacter",
    _UNICODE_NONCHARACTERS,
    ids=lambda value: f"U+{ord(value):04X}",
)
def test_unicode_noncharacters_are_rejected_from_every_project_identity_form(
    tmp_path: Path,
    noncharacter: str,
):
    root = f"{tmp_path.resolve()}{noncharacter}forged"
    slug = f"alpha{noncharacter}beta"
    raw_frontmatter = (
        "---\n"
        f"project_root: {json.dumps(root, ensure_ascii=False)}\n"
        "---\n"
    )
    codepoint = ord(noncharacter)
    escaped = f"\\U{codepoint:08X}"
    escaped_frontmatter = (
        "---\n"
        f'project_root: "{tmp_path.resolve()}{escaped}forged"\n'
        "---\n"
    )

    assert not session_start_project_state.is_canonical_project_slug(slug)
    assert session_start_context._normalize_project_slug(slug) is None
    assert not session_start_project_state._is_native_absolute_root(root)
    assert session_start_context._normalize_project_root(
        json.dumps(root, ensure_ascii=False),
        json_encoded=True,
    ) is None
    assert memory_state.parse_frontmatter_scalar(
        raw_frontmatter,
        "project_root",
    ) == memory_state.FrontmatterScalar(True, None)
    assert memory_state.parse_frontmatter_scalar(
        escaped_frontmatter,
        "project_root",
    ) == memory_state.FrontmatterScalar(True, None)
    assert session_start_project_state._recorded_project_root(
        f"- Project root JSON: {json.dumps(root, ensure_ascii=False)}\n"
    ) is None


@pytest.mark.parametrize("metadata", ("json", "legacy"))
def test_relative_ownership_never_resolves_against_process_cwd(
    monkeypatch,
    tmp_path: Path,
    metadata: str,
):
    projects = tmp_path / "projects"
    state_path = projects / "claimed" / "state.md"
    state_path.parent.mkdir(parents=True)
    cwd = tmp_path / "attacker-cwd"
    cwd.mkdir()
    project = tmp_path / "target"
    project.mkdir()
    monkeypatch.chdir(cwd)
    if metadata == "json":
        ownership = f"- Project root JSON: {json.dumps('../target')}\n"
    else:
        ownership = "- Project root: `../target`\n"
    state_path.write_text(ownership, encoding="utf-8")

    assert _slug_owns_dir("claimed", project, projects) is False
    assert session_start_project_state._recorded_project_root(ownership) is None


def test_relative_json_root_fails_closed_without_absolute_legacy_fallback(
    tmp_path: Path,
):
    project = tmp_path / "target"
    project.mkdir()
    body = (
        f"- Project root JSON: {json.dumps('../target')}\n"
        f"- Project root: `{project.resolve()}`\n"
    )

    assert session_start_project_state._recorded_project_root(body) is None


def test_multiple_or_contradictory_legacy_roots_fail_ownership(tmp_path: Path):
    first = (tmp_path / "first").resolve()
    second = (tmp_path / "second").resolve()

    assert session_start_project_state._recorded_project_root(
        f"- Project root: `{first}`\n- Project root: `{first}`\n"
    ) is None
    assert session_start_project_state._recorded_project_root(
        f"- Project root: `{first}`\n- Project root: `{second}`\n"
    ) is None


@pytest.mark.parametrize(
    "example_wrapper",
    (
        "```markdown\n{metadata}\n```",
        "<!--\n{metadata}\n-->",
    ),
)
def test_state_inventory_ignores_fenced_and_commented_ownership_decoys(
    tmp_path: Path,
    example_wrapper: str,
):
    projects = tmp_path / "projects"
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    state_path = projects / "active" / "state.md"
    state_path.parent.mkdir(parents=True)
    decoy = example_wrapper.format(
        metadata=(
            f"- Project root JSON: {json.dumps(str(other.resolve()))}\n"
            '- Runtime slug JSON: "decoy"'
        )
    )
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "active"\n'
        f"{decoy}\n",
        encoding="utf-8",
    )

    [entry] = session_start_project_state._scan_project_states(projects)

    assert entry.project_root == project.resolve()
    assert entry.runtime_slug == "active"


@pytest.mark.parametrize(
    "example_wrapper",
    (
        "```markdown\n{metadata}\n```",
        "<!--\n{metadata}\n-->",
    ),
)
def test_state_inventory_never_claims_from_fenced_or_commented_examples(
    tmp_path: Path,
    example_wrapper: str,
):
    projects = tmp_path / "projects"
    project = tmp_path / "project"
    project.mkdir()
    state_path = projects / "example-only" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        example_wrapper.format(
            metadata=(
                f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
                '- Runtime slug JSON: "example-only"'
            )
        )
        + "\n",
        encoding="utf-8",
    )

    [entry] = session_start_project_state._scan_project_states(projects)

    assert entry.project_root is None
    assert entry.runtime_slug is None
    assert session_start_project_state.resolve_project_alias(
        "example-only",
        projects,
    ) is None


def test_render_new_state_preserves_invalid_legacy_value_with_canonical_json(
    tmp_path: Path,
):
    template = tmp_path / "state-template.md"
    template.write_text(
        "# <Project Name>\n- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    project = (tmp_path / "project-with-`-marker").resolve()

    rendered = _render_new_state(template, "project", project)

    [json_line] = [
        line for line in rendered.splitlines() if line.startswith("- Project root JSON:")
    ]
    assert json.loads(json_line.split(":", 1)[1].strip()) == str(project)
    assert f"- Project root: `{project}`" in rendered


def test_render_new_state_ignores_fenced_canonical_example_in_legacy_template(
    tmp_path: Path,
):
    template = tmp_path / "state-template.md"
    template.write_text(
        "# <Project Name>\n"
        "```markdown\n"
        '- Project root JSON: "D:/example-only"\n'
        '- Runtime slug JSON: "example-only"\n'
        "```\n"
        "- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"

    rendered = _render_new_state(template, "project", project)

    assert session_start_project_state._recorded_project_root(rendered) == str(project)
    assert session_start_project_state._recorded_runtime_slug(rendered) == "project"
    assert '- Project root JSON: "D:/example-only"' in rendered
    assert "- Project root:" not in rendered


@pytest.mark.parametrize(
    "example_wrapper",
    (
        "```markdown\n{metadata}\n```",
        "<!--\n{metadata}\n-->",
    ),
)
def test_hidden_template_metadata_cannot_block_a_valid_project_claim(
    monkeypatch,
    tmp_path: Path,
    example_wrapper: str,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    hidden = example_wrapper.format(
        metadata=(
            "- Project root: `D:/legacy-example`\n"
            '- Project root JSON: "D:/canonical-example"\n'
            '- Runtime slug JSON: "example-only"'
        )
    )
    template.write_text(
        "# <Project Name>\n"
        f"{hidden}\n"
        "- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    project = tmp_path / "workspace" / "active"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    claimed = session_start_project_state.confirm_project_identity(project, projects)

    assert claimed is not None
    assert claimed[0] == "active"
    assert session_start_project_state._state_path_owns_project(
        claimed[1],
        project.resolve(),
    )


@pytest.mark.parametrize(
    "placeholder",
    (
        "<Project Name>",
        "<what this project is, in one sentence>",
        "<absolute path JSON>",
        "<absolute path>",
        "<remote url>",
    ),
)
def test_render_new_state_round_trips_placeholder_literals_in_project_path(
    tmp_path: Path,
    placeholder: str,
):
    template = tmp_path / "state-template.md"
    template.write_text(
        "# <Project Name>\n"
        "description: <what this project is, in one sentence>\n"
        "- Project root JSON: <absolute path JSON>\n"
        "- Project root: `<absolute path>`\n"
        "- Git remote: `<remote url>`\n",
        encoding="utf-8",
    )
    project = tmp_path / f"project-{placeholder}-literal"

    rendered = _render_new_state(template, "project", project)

    assert session_start_project_state._recorded_project_root(rendered) == str(project)
    assert f"description: (new project at `{project}`, pending description)" in rendered
    assert "- Project root:" not in rendered


def test_session_end_slug_lookup_uses_canonical_json_over_conflicting_legacy_root(
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    state_path = projects / "collision-safe" / "state.md"
    state_path.parent.mkdir(parents=True)
    project = tmp_path / "project-with-`-marker"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project), ensure_ascii=False)}\n"
        f"- Project root: `{other}`\n",
        encoding="utf-8",
    )

    assert session_end_project_tag._lookup_existing_slug(project, projects) == (
        "collision-safe"
    )


def test_session_end_slug_lookup_rejects_malformed_json_root(tmp_path: Path):
    projects = tmp_path / "projects"
    state_path = projects / "unsafe" / "state.md"
    state_path.parent.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    state_path.write_text(
        "- Project root JSON: \"unterminated\n"
        f"- Project root: `{project}`\n",
        encoding="utf-8",
    )

    assert session_end_project_tag._lookup_existing_slug(project, projects) is None


def test_concurrent_same_project_claims_converge_on_one_complete_state(
    monkeypatch, tmp_path: Path
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    project = tmp_path / "workspace" / "service"
    (project / ".git").mkdir(parents=True)

    claim_lock_calls = _synchronize_project_claim_lock(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: session_start_project_state.confirm_project_identity(
                    project, projects
                ),
                range(2),
            )
        )

    assert all(result is not None for result in results)
    claims = [result for result in results if result is not None]
    assert {slug for slug, _path, _is_new in claims} == {"service"}
    assert {state_path for _slug, state_path, _is_new in claims} == {
        projects / "service" / "state.md"
    }
    assert sorted(is_new for _slug, _path, is_new in claims) == [False, True]
    state_files = [
        path for path in projects.glob("*/state.md") if path.parent.name != "_template"
    ]
    assert state_files == [projects / "service" / "state.md"]
    state_body = state_files[0].read_text(encoding="utf-8")
    assert f"- Project root JSON: {json.dumps(str(project.resolve()))}\n" in state_body
    assert "- Project root:" not in state_body
    assert len(claim_lock_calls) == 2


def test_concurrent_project_claims_never_share_state(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    first = tmp_path / "alpha" / "service"
    second = tmp_path / "beta" / "service"
    for project in (first, second):
        (project / ".git").mkdir(parents=True)

    claim_lock_calls = _synchronize_project_claim_lock(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda project: session_start_project_state.confirm_project_identity(
                    project, projects
                ),
                (first, second),
            )
        )

    assert all(result is not None for result in results)
    claims = [result for result in results if result is not None]
    assert len({slug for slug, _path, _is_new in claims}) == 2
    for project, (slug, state_path, is_new) in zip((first, second), claims, strict=True):
        assert is_new is True
        state_body = state_path.read_text(encoding="utf-8")
        assert f"- Project root JSON: {json.dumps(str(project.resolve()))}\n" in state_body
        assert "- Project root:" not in state_body
        assert _slug_owns_dir(slug, project, projects) is True
    assert len(claim_lock_calls) == 2


def test_context_uses_global_scope_when_project_ownership_is_unproven(
    monkeypatch, tmp_path: Path
):
    projects = tmp_path / "projects"
    projects.mkdir()
    active = tmp_path / "active" / "service"
    active.mkdir(parents=True)
    other = tmp_path / "other" / "service"
    other.mkdir(parents=True)
    occupied = projects / "service" / "state.md"
    occupied.parent.mkdir()
    occupied.write_text(
        f"# Other project\n- Project root: `{other}`\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)

    assert session_start_context._resolve_project(active) == (None, None)


def test_context_claim_winner_runs_first_discovery_bootstrap(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    bootstrap = vault / "scripts" / "bootstrap_project.py"
    bootstrap.parent.mkdir()
    bootstrap.write_text(
        "import pathlib, sys\n"
        "project = pathlib.Path(sys.argv[sys.argv.index('--cwd') + 1])\n"
        "(project / 'bootstrap-called').write_text('yes', encoding='utf-8')\n",
        encoding="utf-8",
    )
    project = tmp_path / "active"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)

    session_start_context.build_context(project)
    state_path = projects / "active" / "state.md"

    assert state_path.exists()
    deadline = time.monotonic() + 5
    while not (project / "bootstrap-called").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (project / "bootstrap-called").read_text(encoding="utf-8") == "yes"


def test_context_retries_bootstrap_when_existing_project_has_no_bootstrap(
    monkeypatch, tmp_path: Path
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    project = tmp_path / "active"
    project.mkdir()
    state_path = projects / "active" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        f"# Active\n- Project root: `{project.resolve()}`\n",
        encoding="utf-8",
    )
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)
    monkeypatch.setattr(
        session_start_project_state,
        "_bootstrap_project_state",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert session_start_context._resolve_project(project) == ("active", state_path)
    assert calls == []

    session_start_context.build_context(project)

    assert calls == [
        (
            (vault, project.resolve(), state_path, "active"),
            {"bootstrap_context": ""},
        )
    ]


def test_bootstrap_launcher_spawns_detached_worker(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    script = vault / "scripts" / "bootstrap_project.py"
    script.parent.mkdir(parents=True)
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    project = tmp_path / "active"
    project.mkdir()
    state_path = vault / "knowledge" / "projects" / "active" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("# Active\n", encoding="utf-8")
    launches: list[tuple[list[str], Path | None]] = []
    current_memory_state = sys.modules.get("memory_state", memory_state)
    monkeypatch.setattr(
        current_memory_state,
        "spawn_detached",
        lambda args, cwd=None: launches.append((args, cwd)) or 123,
    )

    session_start_project_state._bootstrap_project_state(vault, project, state_path)

    assert launches == [
        (
            [
                session_start_project_state.sys.executable,
                str(script),
                "--cwd",
                str(project),
                "--apply",
            ],
            vault,
        )
    ]


def test_standalone_project_context_exposes_bootstrap_without_losing_identity(
    tmp_path: Path,
):
    project_root = tmp_path / "alpha-project"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "alpha" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Alpha state\n\n"
        + "STATE_DETAIL_SENTINEL\n"
        + f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        + '- Runtime slug JSON: "alpha"\n',
        encoding="utf-8",
    )
    fingerprint = bootstrap_project._bootstrap_source_fingerprint(project_root, None)
    assert fingerprint is not None
    state_path.with_name("bootstrap.md").write_text(
        "---\ntype: bootstrap-context\n"
        'project_slug_json: "alpha"\n'
        f"project_root_json: {json.dumps(str(project_root.resolve()))}\n"
        f"project_state_path_json: {json.dumps(str(state_path.resolve()))}\n"
        "git_head_json: null\n"
        "bootstrap_schema_json: 2\n"
        f"source_fingerprint_json: {json.dumps(fingerprint)}\n"
        "---\n\n"
        "# Alpha bootstrap\n\nSTANDALONE_BOOTSTRAP_SENTINEL\n"
        + ("x" * 3_000),
        encoding="utf-8",
    )

    context = session_start_project_state._build_context(
        state_path,
        "alpha",
        False,
        project_root,
    )

    assert len(context) <= session_start_project_state.MAX_CONTEXT_CHARS
    assert "# Per-project state" in context
    assert f"- Project root JSON: {json.dumps(str(project_root.resolve()))}" in context
    assert "Project bootstrap" in context
    assert "UNTRUSTED" in context
    assert "STANDALONE_BOOTSTRAP_SENTINEL" in context


def test_project_state_context_names_exact_legacy_state_path_without_moving_it(
    tmp_path: Path,
):
    projects = tmp_path / "vault" / "knowledge" / "projects"
    state_path = projects / "legacy physical folder" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Legacy state\n"
        '- Project root JSON: "D:/active"\n'
        '- Runtime slug JSON: "active-safe"\n',
        encoding="utf-8",
    )

    context = session_start_project_state._build_context(
        state_path,
        "active-safe",
        False,
    )

    assert (
        "Auto-injected from "
        "`knowledge/projects/legacy physical folder/state.md`" in context
    )
    assert "knowledge/projects/active-safe/state.md" not in context
    assert state_path.is_file()
    assert not (projects / "active-safe").exists()


def test_orphan_bootstrap_without_matching_provenance_is_never_injected(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    state_path = tmp_path / "projects" / "active" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "active"\n',
        encoding="utf-8",
    )
    state_path.with_name("bootstrap.md").write_text(
        "---\ntype: bootstrap-context\nproject: active\n---\n\n"
        "# Orphan bootstrap\n\nORPHAN_SENTINEL\n",
        encoding="utf-8",
    )

    context = session_start_project_state._build_context(state_path, "active", False)

    assert "ORPHAN_SENTINEL" not in context
    assert "Project bootstrap" not in context


def test_standalone_project_context_prioritizes_saved_handoff_over_bootstrap(
    tmp_path: Path,
):
    project_root = tmp_path / "alpha-project"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "alpha" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Alpha state\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n\n"
        '- Runtime slug JSON: "alpha"\n\n'
        "## Where we left off\n"
        "SAVED_HANDOFF_SENTINEL\n"
        + ("STATE_DETAIL_TOO_LARGE " + "x" * 20_000 + "\n"),
        encoding="utf-8",
    )
    fingerprint = bootstrap_project._bootstrap_source_fingerprint(project_root, None)
    assert fingerprint is not None
    state_path.with_name("bootstrap.md").write_text(
        "---\ntype: bootstrap-context\n"
        'project_slug_json: "alpha"\n'
        f"project_root_json: {json.dumps(str(project_root.resolve()))}\n"
        f"project_state_path_json: {json.dumps(str(state_path.resolve()))}\n"
        "git_head_json: null\n"
        "bootstrap_schema_json: 2\n"
        f"source_fingerprint_json: {json.dumps(fingerprint)}\n"
        "---\n\n"
        "# Alpha bootstrap\n\n"
        "BOOTSTRAP_MUST_YIELD_SENTINEL\n"
        + ("y" * 20_000),
        encoding="utf-8",
    )

    context = session_start_project_state._build_context(
        state_path,
        "alpha",
        False,
        project_root,
    )

    assert "SAVED_HANDOFF_SENTINEL" in context
    assert "BOOTSTRAP_MUST_YIELD_SENTINEL" not in context


def test_project_state_hook_loser_never_emits_winner_state(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root: `<absolute path>`\n",
        encoding="utf-8",
    )
    first = tmp_path / "alpha" / "service"
    second = tmp_path / "beta" / "service"
    for project in (first, second):
        (project / ".git").mkdir(parents=True)

    active_project = threading.local()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(
        session_start_project_state,
        "_read_hook_payload",
        lambda _stream: {},
    )
    monkeypatch.setattr(
        session_start_project_state,
        "_resolve_project_dir",
        lambda _payload: active_project.value,
    )
    monkeypatch.setattr(session_start_project_state, "_emit", lambda context: context)
    monkeypatch.setattr(session_start_project_state, "_emit_empty", lambda: "")
    monkeypatch.setattr(session_start_project_state, "_safe_write_error", lambda _error: None)

    claim_lock_calls = _synchronize_project_claim_lock(monkeypatch)

    def invoke(project: Path):
        active_project.value = project
        return session_start_project_state.main()

    with ThreadPoolExecutor(max_workers=2) as pool:
        contexts = list(pool.map(invoke, (first, second)))

    for project, other, context in zip(
        (first, second),
        (second, first),
        contexts,
        strict=True,
    ):
        own_root = json.dumps(str(project.resolve()))
        other_root = json.dumps(str(other.resolve()))
        assert f"- Project root JSON: {own_root}" in context
        assert f"- Project root JSON: {other_root}" not in context
    assert len(claim_lock_calls) == 2


# ---------- _compute_slug end-to-end ----------

def test_compute_slug_unique(tmp_path: Path):
    """Clean slug — base strategy wins."""
    projects = tmp_path / "projects"
    projects.mkdir()
    proj = tmp_path / "unique"
    proj.mkdir()
    assert _compute_slug(proj, projects) == "unique"


def test_compute_slug_collision_gets_parent_of_parent(tmp_path: Path):
    """Two projects with the same basename → second gets pop suffix."""
    projects = tmp_path / "projects"
    projects.mkdir()

    # Project A owns "frontend"
    parent_a = tmp_path / "app-a"
    parent_a.mkdir()
    front_a = parent_a / "frontend"
    front_a.mkdir()
    slug_a = _compute_slug(front_a, projects)
    assert slug_a == "frontend"
    # Simulate SessionStart writing state.md
    (projects / slug_a).mkdir()
    (projects / slug_a / "state.md").write_text(
        f"# frontend\n- Project root: `{front_a}`\n", encoding="utf-8"
    )

    # Project B competes
    parent_b = tmp_path / "app-b"
    parent_b.mkdir()
    front_b = parent_b / "frontend"
    front_b.mkdir()
    slug_b = _compute_slug(front_b, projects)
    assert slug_b != slug_a
    # Must use parent-of-parent
    assert slug_b == "frontend-app-b"


def test_compute_slug_idempotent(tmp_path: Path):
    """Re-computing for the same project returns the same slug."""
    projects = tmp_path / "projects"
    projects.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    slug_first = _compute_slug(proj, projects)
    (projects / slug_first).mkdir()
    (projects / slug_first / "state.md").write_text(
        f"# {slug_first}\n- Project root: `{proj}`\n", encoding="utf-8"
    )
    slug_second = _compute_slug(proj, projects)
    assert slug_first == slug_second


def test_posix_case_distinct_project_paths_do_not_share_ownership_key():
    upper = PurePosixPath("/srv/Project")
    lower = PurePosixPath("/srv/project")

    assert session_start_project_state._path_comparison_key(upper, "linux") != (
        session_start_project_state._path_comparison_key(lower, "linux")
    )
    assert session_start_project_state._path_comparison_key(upper, "win32") == (
        session_start_project_state._path_comparison_key(lower, "win32")
    )


def test_windows_project_path_key_normalizes_case_and_separators():
    first = PureWindowsPath(r"C:\Workspace\Project")
    equivalent = PureWindowsPath("c:/workspace/project")
    different = PureWindowsPath("c:/workspace/other")

    assert session_start_project_state._path_comparison_key(first, "win32") == (
        session_start_project_state._path_comparison_key(equivalent, "win32")
    )
    assert session_start_project_state._path_comparison_key(first, "win32") != (
        session_start_project_state._path_comparison_key(different, "win32")
    )


def test_project_root_resolver_keeps_nested_cwd_on_trusted_root(tmp_path: Path):
    project = tmp_path / "project"
    nested = project / "src" / "package"
    nested.mkdir(parents=True)

    resolved = session_start_project_state.resolve_project_root(
        {"cwd": str(nested)},
        env={"CLAUDE_PROJECT_DIR": str(project)},
    )

    assert resolved.root == project.resolve()
    assert resolved.signal_present is True


def test_project_root_resolver_fails_closed_on_conflicting_root_fields(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    resolved = session_start_project_state.resolve_project_root(
        {
            "project_dir": str(first),
            "project_root": str(second),
            "cwd": str(first),
        },
        env={},
    )

    assert resolved.root is None
    assert resolved.signal_present is True


def test_project_state_hook_uses_root_signal_instead_of_nested_cwd(tmp_path: Path):
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)

    resolved = session_start_project_state._resolve_project_dir(
        {"cwd": str(nested)},
        {"CLAUDE_PROJECT_DIR": str(project)},
    )

    assert resolved == project.resolve()


def test_codex_root_resolution_rejects_conflicting_agent_root(
    monkeypatch,
    tmp_path: Path,
):
    import codex_memory

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("CODEX_PROJECT_DIR", str(first))

    assert codex_memory._project_dir(str(second)) is None


def test_project_root_resolver_handles_cwd_changed_without_fragmenting_identity(
    tmp_path: Path,
):
    project = tmp_path / "project"
    old_cwd = project / "src"
    new_cwd = project / "tests" / "unit"
    old_cwd.mkdir(parents=True)
    new_cwd.mkdir(parents=True)

    resolved = session_start_project_state.resolve_project_root(
        {
            "hook_event_name": "CwdChanged",
            "cwd": str(new_cwd),
            "old_cwd": str(old_cwd),
            "new_cwd": str(new_cwd),
        },
        env={"CLAUDE_PROJECT_DIR": str(project)},
    )

    assert resolved == session_start_project_state.ProjectRootResolution(
        project.resolve(),
        True,
    )


def test_project_root_resolver_distinguishes_absence_from_invalid_presence(
    tmp_path: Path,
):
    absent = session_start_project_state.resolve_project_root({}, env={})
    invalid = session_start_project_state.resolve_project_root(
        {"project_root": "relative/project"},
        env={},
    )

    assert absent == session_start_project_state.ProjectRootResolution(None, False)
    assert invalid == session_start_project_state.ProjectRootResolution(None, True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
def test_project_root_resolver_uses_windows_case_insensitive_containment(
    tmp_path: Path,
):
    project = tmp_path / "CaseSensitiveName"
    nested = project / "Nested"
    nested.mkdir(parents=True)

    resolved = session_start_project_state.resolve_project_root(
        {
            "project_root": str(project).upper(),
            "cwd": str(nested).lower(),
        },
        env={},
    )

    assert resolved.root is not None
    assert session_start_project_state._path_comparison_key(resolved.root) == (
        session_start_project_state._path_comparison_key(project.resolve())
    )


def test_resolve_project_state_rejects_resolved_path_outside_projects(
    monkeypatch, tmp_path: Path
):
    projects = tmp_path / "projects"
    projects.mkdir()
    project = tmp_path / "active"
    project.mkdir()
    monkeypatch.setattr(
        session_start_project_state,
        "_allocate_slug",
        lambda _project, _projects, _reserved: "../escaped",
    )

    with pytest.raises(ValueError, match="outside knowledge/projects"):
        session_start_project_state.resolve_project_state(project, projects)

    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)
    assert session_start_context._resolve_project(project) == (None, None)


def test_resolve_project_state_rejects_slug_symlink_escape(tmp_path: Path):
    projects = tmp_path / "projects"
    projects.mkdir()
    project = tmp_path / "active"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.md").write_text(
        f"# Escaped state\n- Project root: `{project}`\n",
        encoding="utf-8",
    )
    link = projects / "active"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this platform")

    slug, state_path = session_start_project_state.resolve_project_state(
        project, projects
    )

    assert slug != "active"
    assert state_path.resolve().is_relative_to(projects.resolve())


def test_compute_slug_hash_suffix_last_resort(tmp_path: Path):
    """If base, pop, git, and grandparent all collide, hash suffix kicks in."""
    projects = tmp_path / "projects"
    projects.mkdir()

    # Create a project dir with no git and no meaningful parents
    proj = tmp_path / "orphan"
    proj.mkdir()

    # Pre-occupy every candidate the resolver would try
    for slug in ["orphan"]:
        (projects / slug).mkdir()
        (projects / slug / "state.md").write_text(
            f"# {slug}\n- Project root: `/somewhere/else`\n", encoding="utf-8"
        )

    # With no parent-of-parent matching, it should still resolve — either
    # via grandparent (tmp_path.name) or via hash. The output must NOT be
    # bare "orphan" (that's taken).
    slug = _compute_slug(proj, projects)
    assert slug != "orphan"
    assert slug.startswith("orphan") or slug == "root"


def test_exhausted_deterministic_candidates_use_a_verified_uuid_suffix(
    monkeypatch,
    tmp_path: Path,
):
    projects = tmp_path / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    project = tmp_path / "grand" / "parent" / "service"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@example.test:owner/repository.git\n',
        encoding="utf-8",
    )
    other = tmp_path / "other-owner"
    other.mkdir()
    digest = hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()
    deterministic = {
        "service",
        "service-parent",
        "owner-repository",
        "service-grand",
        *(f"service-{digest[:length]}" for length in (6, 12, 24, 40, 64)),
    }
    first_uuid_slug = f"service-{'a' * 32}"
    for slug in (*sorted(deterministic), first_uuid_slug):
        state_path = projects / slug / "state.md"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            f"- Project root JSON: {json.dumps(str(other.resolve()))}\n",
            encoding="utf-8",
        )
    generated = iter(("a" * 32, "b" * 32))
    uuid_calls: list[str] = []

    class FakeUuid:
        def __init__(self, value: str):
            self.hex = value

    def fake_uuid4():
        value = next(generated)
        uuid_calls.append(value)
        return FakeUuid(value)

    monkeypatch.setattr(session_start_project_state, "uuid4", fake_uuid4, raising=False)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    claimed = session_start_project_state.confirm_project_identity(project, projects)

    assert claimed is not None
    slug, state_path, is_new = claimed
    assert slug == f"service-{'b' * 32}"
    assert slug not in deterministic
    assert session_start_project_state.is_canonical_project_slug(slug)
    assert is_new is True
    assert uuid_calls == ["a" * 32, "b" * 32]
    assert session_start_project_state._recorded_project_root(
        state_path.read_text(encoding="utf-8")
    ) == str(project.resolve())
