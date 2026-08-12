from __future__ import annotations

import errno
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import bootstrap_project
import pytest
import session_start_context
import session_start_project_state

HANDOFF_PLACEHOLDER = (
    '<Most recent context — "we were working on X, stopped at Y, next step is Z". '
    "Keep to ≤ 5 bullets. This is what gets injected at session start, so it should "
    "read like a handoff note to future-you.>"
)
DECISIONS_PLACEHOLDER = (
    "<Architectural or scope decisions specific to this project. Include the *why*. "
    "Cross-cutting decisions belong in `knowledge/notes/`, not here.>"
)
THREADS_PLACEHOLDER = (
    "<Unresolved questions, pending investigations, TODOs that need context to "
    "understand. Close them when resolved.>"
)
LINKS_PLACEHOLDER = (
    "<Wikilinks to related pages in this vault: concepts used, sibling projects, raw "
    "sources. Wikilinks only — external URLs belong inside the content above with "
    "context.>"
)
EDITORIAL_TEMPLATE = (
    "This page is per-project state — **read and auto-created** by the SessionStart "
    "hook when a markered folder is opened; its **content** is edited by Claude or "
    "the user during/after the session (the SessionEnd hook only tags the shared "
    "daily log, it does not write state.md). Content decisions (what to keep, what "
    "to archive) follow [[Global Multi-Project Migration Plan]] conventions. Keep "
    "this page to ≤ 1 screen; move detail into sibling pages under the same project "
    "folder."
)


def _write_state(
    state_path: Path,
    project_root: Path,
    slug: str,
    content: str,
    *,
    root_line: str | None = None,
    runtime_line: str | None = None,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    root = (
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}"
        if root_line is None
        else root_line
    )
    runtime = (
        f"- Runtime slug JSON: {json.dumps(slug)}"
        if runtime_line is None
        else runtime_line
    )
    state_path.write_text(
        f"# {slug} state\n{root}\n{runtime}\n{content.rstrip()}\n",
        encoding="utf-8",
    )


def _write_bootstrap(
    state_path: Path,
    project_root: Path,
    slug: str,
    body: str,
    *,
    git_head: str | None | object = None,
    include_git_head: bool = True,
) -> None:
    head_line = ""
    if include_git_head:
        head_line = f"git_head_json: {json.dumps(git_head)}\n"
    fingerprint = bootstrap_project._bootstrap_source_fingerprint(
        project_root,
        git_head if isinstance(git_head, str) else None,
    )
    assert fingerprint is not None
    state_path.with_name("bootstrap.md").write_text(
        "---\n"
        "type: bootstrap-context\n"
        f"project_slug_json: {json.dumps(slug)}\n"
        f"project_root_json: {json.dumps(str(project_root.resolve()))}\n"
        f"project_state_path_json: {json.dumps(str(state_path.resolve()))}\n"
        f"{head_line}"
        "bootstrap_schema_json: 2\n"
        f"source_fingerprint_json: {json.dumps(fingerprint)}\n"
        "---\n\n"
        f"# {slug} bootstrap\n\n{body}\n",
        encoding="utf-8",
    )


def _run_hook(
    monkeypatch,
    capsys,
    vault: Path,
    project_root: Path,
    *,
    claimed: tuple[str, Path, bool] | None | object = None,
) -> str:
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(vault / "runtime"))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "project_dir": str(project_root),
                    "cwd": str(project_root),
                }
            )
        ),
    )
    monkeypatch.setattr(
        session_start_project_state,
        "_bootstrap_project_state",
        lambda *_args, **_kwargs: None,
    )
    if claimed is not None:
        monkeypatch.setattr(
            session_start_project_state,
            "confirm_project_identity",
            lambda *_args, **_kwargs: claimed,
        )

    assert session_start_project_state.main() == 0
    payload = json.loads(capsys.readouterr().out)
    return payload["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    ("root_line", "runtime_line"),
    (
        pytest.param(
            "legacy",
            "valid",
            id="legacy-absolute-bank-list-root",
        ),
        pytest.param("invalid-bank-list", "valid", id="invalid-bank-list-root"),
        pytest.param("valid", "missing", id="missing-runtime-slug"),
        pytest.param("valid", "other", id="wrong-runtime-slug"),
        pytest.param("other-root", "valid", id="wrong-confirmed-root"),
        pytest.param("duplicate-json", "valid", id="duplicate-json-root"),
    ),
)
def test_saved_handoff_requires_complete_confirmed_ownership_tuple(
    monkeypatch,
    capsys,
    tmp_path: Path,
    root_line: str,
    runtime_line: str,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "bank-list"
    other = tmp_path / "other" / "bank-list"
    project.mkdir(parents=True)
    other.mkdir(parents=True)
    state_path = projects / "bank-list" / "state.md"
    roots = {
        "legacy": f"- Project root: `{project.resolve()}`",
        "invalid-bank-list": "- Project root: bank-list",
        "valid": f"- Project root JSON: {json.dumps(str(project.resolve()))}",
        "other-root": f"- Project root JSON: {json.dumps(str(other.resolve()))}",
        "duplicate-json": (
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
            f"- Project root JSON: {json.dumps(str(project.resolve()))}"
        ),
    }
    runtimes = {
        "valid": '- Runtime slug JSON: "bank-list"',
        "missing": "",
        "other": '- Runtime slug JSON: "other-project"',
    }
    _write_state(
        state_path,
        project,
        "bank-list",
        "## Where we left off\nCROSS_PROJECT_HANDOFF_MUST_NOT_APPEAR",
        root_line=roots[root_line],
        runtime_line=runtimes[runtime_line],
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("bank-list", state_path, False),
    )

    assert "CROSS_PROJECT_HANDOFF_MUST_NOT_APPEAR" not in context
    assert "## Where we left off" not in context


def test_valid_confirmed_handoff_is_injected(monkeypatch, capsys, tmp_path: Path):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "active"
    project.mkdir(parents=True)
    state_path = projects / "legacy physical folder" / "state.md"
    _write_state(
        state_path,
        project,
        "active-safe",
        "## Where we left off\nVALID_CURRENT_HANDOFF",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("active-safe", state_path, False),
    )

    assert "VALID_CURRENT_HANDOFF" in context


@pytest.mark.parametrize(
    "legacy_history",
    (
        "- Project root: `{root}`",
        (
            "- Project root: `{root}`\n"
            "- Project root migration note: preserve shipped ownership prose."
        ),
    ),
    ids=("shipped-backtick-root", "shipped-backtick-and-prose"),
)
def test_canonical_state_tuple_ignores_shipped_legacy_root_history(
    monkeypatch,
    capsys,
    tmp_path: Path,
    legacy_history: str,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "shipped-state"
    project.mkdir(parents=True)
    state_path = projects / "shipped-state" / "state.md"
    _write_state(
        state_path,
        project,
        "shipped-state",
        f"{legacy_history.format(root=project.resolve())}\n\n"
        "## Where we left off\nCANONICAL_TUPLE_HANDOFF",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("shipped-state", state_path, False),
    )

    assert "CANONICAL_TUPLE_HANDOFF" in context


@pytest.mark.parametrize(
    "prose",
    (
        "- Project root JSON migration notes remain ordinary project content.",
        "- Runtime slug JSON migration notes remain ordinary project content.",
        "- Project root migration notes remain ordinary project content.",
    ),
    ids=("json-root-key-prefix", "runtime-slug-key-prefix", "legacy-root-key-prefix"),
)
def test_ownership_key_prefix_prose_is_preserved_without_invalidating_trust(
    monkeypatch,
    capsys,
    tmp_path: Path,
    prose: str,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "ownership-prose"
    project.mkdir(parents=True)
    state_path = projects / "ownership-prose" / "state.md"
    _write_state(
        state_path,
        project,
        "ownership-prose",
        f"{prose}\n\n## Where we left off\nTRUSTED_HANDOFF_WITH_PREFIX_PROSE",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("ownership-prose", state_path, False),
    )

    assert prose in context
    assert "TRUSTED_HANDOFF_WITH_PREFIX_PROSE" in context


@pytest.mark.parametrize(
    "prose",
    (
        "- Project root JSON: migration prose is not a JSON string.",
        "- Runtime slug JSON: migration prose is not a JSON string.",
        "- Project root: migration prose is not a backtick path.",
    ),
    ids=("invalid-json-root-value", "invalid-runtime-value", "invalid-legacy-value"),
)
def test_invalid_ownership_value_prose_is_preserved_without_invalidating_trust(
    monkeypatch,
    capsys,
    tmp_path: Path,
    prose: str,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "ownership-value-prose"
    project.mkdir(parents=True)
    state_path = projects / "ownership-value-prose" / "state.md"
    _write_state(
        state_path,
        project,
        "ownership-value-prose",
        f"{prose}\n\n## Where we left off\nTRUSTED_HANDOFF_WITH_VALUE_PROSE",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("ownership-value-prose", state_path, False),
    )

    assert prose in context
    assert "TRUSTED_HANDOFF_WITH_VALUE_PROSE" in context


def test_large_identity_metadata_cannot_clip_standalone_saved_handoff(tmp_path: Path):
    project = tmp_path / "work" / "bounded-standalone"
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / "bounded-standalone" / "state.md"
    metadata = "ARBITRARY_IDENTITY_METADATA_" + ("x" * 2_300)
    _write_state(
        state_path,
        project,
        "bounded-standalone",
        f"## Metadata\n{metadata}\n\n"
        "## Where we left off\n"
        "RESERVED_STANDALONE_HANDOFF",
    )

    context = session_start_project_state._build_context(
        state_path,
        "bounded-standalone",
        False,
        project,
    )

    assert len(context) <= session_start_project_state.MAX_CONTEXT_CHARS
    assert "RESERVED_STANDALONE_HANDOFF" in context
    if "ARBITRARY_IDENTITY_METADATA_" in context:
        assert context.index("RESERVED_STANDALONE_HANDOFF") < context.index(
            "ARBITRARY_IDENTITY_METADATA_"
        )


def test_state_authored_h1_cannot_precede_or_clip_canonical_identity_and_handoff(
    tmp_path: Path,
):
    project = tmp_path / "work" / "oversized-heading"
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / "oversized-heading" / "state.md"
    state_path.parent.mkdir(parents=True)
    authored_heading = "# AUTHORED_H1_" + ("h" * 5_000)
    state_path.write_text(
        f"{authored_heading}\n"
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "oversized-heading"\n'
        "## Where we left off\n"
        "OVERSIZED_H1_MUST_NOT_CLIP_HANDOFF\n",
        encoding="utf-8",
    )

    context = session_start_project_state._build_context(
        state_path,
        "oversized-heading",
        False,
        project,
    )

    root_line = f"- Project root JSON: {json.dumps(str(project.resolve()))}"
    slug_line = '- Runtime slug JSON: "oversized-heading"'
    assert len(context) <= session_start_project_state.MAX_CONTEXT_CHARS
    assert f"{root_line}\n{slug_line}" in context
    assert "OVERSIZED_H1_MUST_NOT_CLIP_HANDOFF" in context
    assert context.index(root_line) < context.index("AUTHORED_H1_")
    assert context.index(slug_line) < context.index("AUTHORED_H1_")
    assert context.index("OVERSIZED_H1_MUST_NOT_CLIP_HANDOFF") < context.index(
        "AUTHORED_H1_"
    )


def test_standalone_context_omits_when_required_marker_cannot_fit(
    tmp_path: Path,
):
    slug = "long-windows"
    project = tmp_path / "work" / slug
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / slug / "state.md"
    state_path.parent.mkdir(parents=True)
    header = (
        f"# Per-project state — `{slug}`\n\n"
        f"(Auto-injected from "
        f"`{session_start_project_state._state_context_source(state_path)}`.)"
    )
    marker = session_start_project_state.CONTEXT_TRUNCATION_MARKER
    selected: tuple[str, str, str] | None = None
    for segment_count in range(1, 1_000):
        windows_root = "C:\\" + "\\".join(["segment"] * segment_count)
        root_line = f"- Project root JSON: {json.dumps(windows_root)}"
        slug_line = f"- Runtime slug JSON: {json.dumps(slug)}"
        identity = f"{root_line}\n{slug_line}"
        mandatory = f"{header}\n\n{identity}"
        if len(mandatory) + 1 > session_start_project_state.MAX_CONTEXT_CHARS:
            break
        selected = root_line, slug_line, mandatory
    assert selected is not None
    root_line, slug_line, mandatory = selected
    assert len(mandatory) + 1 <= session_start_project_state.MAX_CONTEXT_CHARS
    assert (
        len(f"{mandatory}\n\n{marker}\n")
        > session_start_project_state.MAX_CONTEXT_CHARS
    )
    snapshot = session_start_project_state.ProjectStateContextSnapshot(
        state_path=state_path,
        slug=slug,
        project_root=project.resolve(),
        trusted_state_body="trusted synthetic state",
        trusted_state_parts=(
            f"{root_line}\n{slug_line}",
            "## Where we left off\nWINDOWS_HANDOFF_" + ("h" * 800),
            "# Authored state\nWINDOWS_SECONDARY_DETAIL",
        ),
        bootstrap="WINDOWS_BOOTSTRAP_" + ("b" * 800),
    )

    context = session_start_project_state._build_context(
        state_path,
        slug,
        False,
        project.resolve(),
        snapshot=snapshot,
    )

    assert context == ""


def test_standalone_context_emits_complete_short_secondary_at_exact_boundary(
    tmp_path: Path,
):
    slug = "exact-short-secondary"
    project = tmp_path / "work" / slug
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / slug / "state.md"
    state_path.parent.mkdir(parents=True)
    header = (
        f"# Per-project state — `{slug}`\n\n"
        f"(Auto-injected from "
        f"`{session_start_project_state._state_context_source(state_path)}`.)"
    )
    secondary = "EXACT"
    assert len(secondary) < len(
        session_start_project_state.CONTEXT_TRUNCATION_MARKER
    )
    root_prefix = '- Project root JSON: "/'
    identity_suffix = f'"\n- Runtime slug JSON: {json.dumps(slug)}'
    base_identity = f"{root_prefix}{identity_suffix}"
    base_context = f"{header}\n\n{base_identity}\n\n{secondary}\n"
    filler_size = session_start_project_state.MAX_CONTEXT_CHARS - len(base_context)
    assert filler_size > 0
    identity = f'{root_prefix}{"s" * filler_size}{identity_suffix}'
    mandatory = f"{header}\n\n{identity}"
    expected = f"{mandatory}\n\n{secondary}\n"
    marker_context = (
        f"{mandatory}\n\n"
        f"{session_start_project_state.CONTEXT_TRUNCATION_MARKER}\n"
    )
    assert len(expected) == session_start_project_state.MAX_CONTEXT_CHARS
    assert len(marker_context) > session_start_project_state.MAX_CONTEXT_CHARS
    snapshot = session_start_project_state.ProjectStateContextSnapshot(
        state_path=state_path,
        slug=slug,
        project_root=project.resolve(),
        trusted_state_body="trusted synthetic state",
        trusted_state_parts=(identity, secondary, ""),
        bootstrap="",
    )

    context = session_start_project_state._build_context(
        state_path,
        slug,
        False,
        project.resolve(),
        snapshot=snapshot,
    )

    assert context == expected
    assert session_start_project_state.CONTEXT_TRUNCATION_MARKER not in context


def test_standalone_context_omits_project_when_identity_exceeds_total_budget(
    tmp_path: Path,
):
    slug = "over-total-standalone"
    project = tmp_path / "work" / slug
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / slug / "state.md"
    state_path.parent.mkdir(parents=True)
    root_prefix = "OVER_TOTAL_STANDALONE_ROOT_"
    root_line = f'- Project root JSON: "/{root_prefix}{"x" * 3_000}"'
    slug_line = f"- Runtime slug JSON: {json.dumps(slug)}"
    snapshot = session_start_project_state.ProjectStateContextSnapshot(
        state_path=state_path,
        slug=slug,
        project_root=project.resolve(),
        trusted_state_body="trusted synthetic state",
        trusted_state_parts=(f"{root_line}\n{slug_line}", "", ""),
        bootstrap="",
    )

    context = session_start_project_state._build_context(
        state_path,
        slug,
        False,
        project.resolve(),
        snapshot=snapshot,
    )

    assert context == ""


def test_standalone_context_preserves_identity_before_clipped_unicode_content(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "work" / "unicode-standalone"
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / "unicode-standalone" / "state.md"
    handoff = "HANDOFF_START_" + ("界🙂" * 350) + "_HANDOFF_END"
    bootstrap = "BOOTSTRAP_START_" + ("λ🚀" * 900) + "_BOOTSTRAP_END"
    _write_state(
        state_path,
        project,
        "unicode-standalone",
        f"## Where we left off\n{handoff}",
    )
    monkeypatch.setattr(
        session_start_project_state,
        "_read_bootstrap_context",
        lambda *_args, **_kwargs: bootstrap,
    )

    context = session_start_project_state._build_context(
        state_path,
        "unicode-standalone",
        False,
        project,
    )

    root_line = f"- Project root JSON: {json.dumps(str(project.resolve()))}"
    slug_line = '- Runtime slug JSON: "unicode-standalone"'
    assert len(context) <= session_start_project_state.MAX_CONTEXT_CHARS
    assert root_line in context
    assert slug_line in context
    assert context.index(root_line) < context.index("HANDOFF_START_")
    assert context.index(slug_line) < context.index("HANDOFF_START_")
    assert "_HANDOFF_END" in context
    assert "BOOTSTRAP_START_" in context
    assert "_BOOTSTRAP_END" not in context
    assert "... (line truncated)" in context
    assert "... (context truncated)" in context
    assert context.encode("utf-8").decode("utf-8") == context


def test_standalone_context_reads_trusted_state_once(monkeypatch, tmp_path: Path):
    project = tmp_path / "work" / "standalone-cache"
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / "standalone-cache" / "state.md"
    _write_state(
        state_path,
        project,
        "standalone-cache",
        "## Where we left off\nCACHED_STANDALONE_HANDOFF",
    )
    _write_bootstrap(
        state_path,
        project,
        "standalone-cache",
        "CACHED_STANDALONE_BOOTSTRAP",
    )
    reads = 0
    real_reader = session_start_project_state._read_trusted_state_body

    def counted_reader(*args, **kwargs):
        nonlocal reads
        reads += 1
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(
        session_start_project_state,
        "_read_trusted_state_body",
        counted_reader,
    )

    context = session_start_project_state._build_context(
        state_path,
        "standalone-cache",
        False,
        project,
    )

    assert reads == 1
    assert "CACHED_STANDALONE_HANDOFF" in context
    assert "CACHED_STANDALONE_BOOTSTRAP" in context


def test_large_identity_metadata_cannot_clip_combined_saved_handoff(tmp_path: Path):
    project = tmp_path / "work" / "bounded-combined"
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / "bounded-combined" / "state.md"
    metadata = "ARBITRARY_COMBINED_METADATA_" + ("x" * 2_300)
    _write_state(
        state_path,
        project,
        "bounded-combined",
        f"## Metadata\n{metadata}\n\n"
        "## Where we left off\n"
        "RESERVED_COMBINED_HANDOFF",
    )

    block = session_start_context._project_state_block(
        "bounded-combined",
        state_path,
        project,
    )
    bounded = session_start_context._bounded_block(
        block,
        session_start_context.SECTION_BUDGETS["project"],
    )

    assert len(bounded) <= session_start_context.SECTION_BUDGETS["project"]
    assert "RESERVED_COMBINED_HANDOFF" in bounded
    if "ARBITRARY_COMBINED_METADATA_" in bounded:
        assert bounded.index("RESERVED_COMBINED_HANDOFF") < bounded.index(
            "ARBITRARY_COMBINED_METADATA_"
        )


def test_seven_template_project_states_have_no_saved_handoff(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "tools-agent"
    project.mkdir(parents=True)
    state_path = projects / "tools-agent" / "state.md"
    template_sections = (
        "## Where we left off\n"
        f"{HANDOFF_PLACEHOLDER}\n\n"
        "## Recent decisions\n"
        f"{DECISIONS_PLACEHOLDER}\n\n"
        "## Open threads\n"
        f"{THREADS_PLACEHOLDER}\n\n"
        "## Links\n"
        f"{LINKS_PLACEHOLDER}\n\n"
        "## Editorial note\n"
        f"{EDITORIAL_TEMPLATE}"
    )

    for index in range(7):
        slug = f"placeholder-{index}"
        _write_state(
            state_path,
            project,
            slug,
            "One-sentence summary: "
            f"(new project at `{project.resolve()}`, pending description).\n\n"
            f"{template_sections}",
        )
        context = _run_hook(
            monkeypatch,
            capsys,
            vault,
            project,
            claimed=(slug, state_path, False),
        )

        assert "<Most recent context" not in context
        assert "<Architectural or scope" not in context
        assert "<Unresolved questions" not in context
        assert "<Wikilinks to related" not in context
        assert "pending description" not in context
        assert "## Where we left off" not in context
        assert "Saved project state" not in context


def test_new_state_pending_description_with_backtick_root_is_suppressed(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n"
        "One-sentence summary: <what this project is, in one sentence>\n"
        "- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    project = tmp_path / "work" / "project-`-root"
    (project / ".git").mkdir(parents=True)

    context = _run_hook(monkeypatch, capsys, vault, project)

    [state_path] = [
        path
        for path in projects.glob("*/state.md")
        if path.parent.name != "_template"
    ]
    assert "pending description" in state_path.read_text(encoding="utf-8")
    assert "pending description" not in context
    assert "Saved project state" not in context


def test_exact_placeholder_text_in_code_or_real_prose_is_preserved(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "angle-code"
    project.mkdir(parents=True)
    state_path = projects / "angle-code" / "state.md"
    _write_state(
        state_path,
        project,
        "angle-code",
        "## Where we left off\n"
        "- Keep `Array<T>` and the phrase <Most recent context is user data>.\n\n"
        "## Recent decisions\n"
        "```text\n"
        f"{DECISIONS_PLACEHOLDER}\n"
        "```",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("angle-code", state_path, False),
    )

    assert "Array<T>" in context
    assert "<Most recent context is user data>" in context
    assert DECISIONS_PLACEHOLDER in context


def test_code_examples_cannot_forge_or_invalidate_state_ownership(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "ownership-code"
    project.mkdir(parents=True)
    state_path = projects / "ownership-code" / "state.md"
    _write_state(
        state_path,
        project,
        "ownership-code",
        "## Where we left off\nREAL_HANDOFF_SURVIVES_CODE_EXAMPLE\n\n"
        "## Recent decisions\n"
        "```markdown\n"
        '- Project root JSON: "D:/decoy"\n'
        '- Runtime slug JSON: "decoy"\n'
        "```",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("ownership-code", state_path, False),
    )

    assert "REAL_HANDOFF_SURVIVES_CODE_EXAMPLE" in context
    assert '- Project root JSON: "D:/decoy"' in context


@pytest.mark.parametrize(
    "hidden_example",
    (
        "```markdown\n## Where we left off\nFENCED_HANDOFF_DECOY\n```",
        "<!--\n## Where we left off\nCOMMENTED_HANDOFF_DECOY\n-->",
    ),
)
def test_hidden_handoff_heading_never_suppresses_visible_handoff(
    monkeypatch,
    capsys,
    tmp_path: Path,
    hidden_example: str,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "hidden-heading"
    project.mkdir(parents=True)
    state_path = projects / "hidden-heading" / "state.md"
    _write_state(
        state_path,
        project,
        "hidden-heading",
        f"{hidden_example}\n\n"
        "## Where we left off\n"
        "VISIBLE_HANDOFF_MUST_SURVIVE",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("hidden-heading", state_path, False),
    )

    assert "VISIBLE_HANDOFF_MUST_SURVIVE" in context


@pytest.mark.parametrize(
    "hidden_heading",
    (
        "```markdown\n## Recent decisions\n```",
        "<!-- ## Recent decisions -->",
    ),
)
def test_hidden_heading_never_terminates_visible_handoff(
    tmp_path: Path,
    hidden_heading: str,
):
    project = tmp_path / "work" / "hidden-section-break"
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / "hidden-section-break" / "state.md"
    _write_state(
        state_path,
        project,
        "hidden-section-break",
        "## Where we left off\n"
        "HANDOFF_BEFORE_HIDDEN_HEADING\n"
        f"{hidden_heading}\n"
        "HANDOFF_AFTER_HIDDEN_HEADING\n\n"
        "## Recent decisions\n"
        "VISIBLE_DECISION",
    )

    trusted = session_start_project_state._read_trusted_state_parts(
        state_path,
        "hidden-section-break",
        project,
    )

    assert trusted is not None
    assert "HANDOFF_BEFORE_HIDDEN_HEADING" in trusted[1]
    assert "HANDOFF_AFTER_HIDDEN_HEADING" in trusted[1]
    assert "VISIBLE_DECISION" in trusted[2]


@pytest.mark.parametrize(
    "raw_block",
    (
        "<script>\n{hidden}\n</script>",
        "<!--\n{hidden}\n-->",
        "<?processing\n{hidden}\n?>",
        "<!A declaration\n{hidden}\n>",
        "<![CDATA[\n{hidden}\n]]>",
        "<div>\n{hidden}\n\n",
        '<custom data-kind="state">\n{hidden}\n\n',
    ),
    ids=(
        "type-1",
        "type-2",
        "type-3",
        "type-4",
        "type-5",
        "type-6",
        "type-7",
    ),
)
def test_commonmark_raw_html_cannot_supply_ownership_or_hidden_headings(
    tmp_path: Path,
    raw_block: str,
):
    project = tmp_path / "work" / "raw-html-state"
    other = tmp_path / "work" / "decoy"
    project.mkdir(parents=True)
    other.mkdir(parents=True)
    state_path = tmp_path / "projects" / "raw-html-state" / "state.md"
    state_path.parent.mkdir(parents=True)
    hidden_ownership = raw_block.format(
        hidden=(
            f"- Project root JSON: {json.dumps(str(other.resolve()))}\n"
            '- Runtime slug JSON: "decoy"'
        )
    )
    hidden_heading = raw_block.format(hidden="## Recent decisions\nHIDDEN_DECISION")
    state_path.write_text(
        "# Raw HTML state\n"
        f"{hidden_ownership}\n"
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "raw-html-state"\n'
        "## Where we left off\n"
        "HANDOFF_BEFORE_RAW_HTML\n"
        f"{hidden_heading}\n"
        "HANDOFF_AFTER_RAW_HTML\n"
        "## Recent decisions\n"
        "VISIBLE_DECISION\n",
        encoding="utf-8",
    )

    trusted = session_start_project_state._read_trusted_state_parts(
        state_path,
        "raw-html-state",
        project,
    )

    assert trusted is not None
    assert "HANDOFF_BEFORE_RAW_HTML" in trusted[1]
    assert "HANDOFF_AFTER_RAW_HTML" in trusted[1]
    assert "VISIBLE_DECISION" in trusted[2]


def test_non_template_editorial_content_is_preserved(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "editorial-content"
    project.mkdir(parents=True)
    state_path = projects / "editorial-content" / "state.md"
    _write_state(
        state_path,
        project,
        "editorial-content",
        "## Editorial note\n"
        "Preserve PROJECT_EDITORIAL_<T> because it records a real local convention.",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("editorial-content", state_path, False),
    )

    assert "PROJECT_EDITORIAL_<T>" in context


def test_placeholder_plus_stale_freecad_process_metadata_is_not_a_handoff(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "freecad"
    project.mkdir(parents=True)
    state_path = projects / "freecad" / "state.md"

    def dead_process(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", dead_process)
    _write_state(
        state_path,
        project,
        "freecad",
        "## Where we left off\n"
        f"{HANDOFF_PLACEHOLDER}\n"
        "- FreeCAD GUI process PID 999999 is still listed as active.\n"
        "- Last updated: 2025-01-01T00:00:00Z",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("freecad", state_path, False),
    )

    assert "<Most recent context" not in context
    assert "PID 999999" not in context
    assert "Last updated" not in context
    assert "## Where we left off" not in context
    assert "Saved project state" not in context


def test_bare_dead_freecad_pid_status_is_not_a_handoff(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "freecad-bare-pid"
    project.mkdir(parents=True)
    state_path = projects / "freecad-bare-pid" / "state.md"

    def dead_process(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", dead_process)
    _write_state(
        state_path,
        project,
        "freecad-bare-pid",
        "## Where we left off\n- FreeCAD GUI PID: 999999",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("freecad-bare-pid", state_path, False),
    )

    assert "PID: 999999" not in context
    assert "## Where we left off" not in context


def test_standalone_dead_pid_is_not_a_handoff_when_section_has_nothing_else(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "standalone-dead-pid"
    project.mkdir(parents=True)
    state_path = projects / "standalone-dead-pid" / "state.md"

    def dead_process(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", dead_process)
    _write_state(
        state_path,
        project,
        "standalone-dead-pid",
        "## Where we left off\nPID: 999999",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("standalone-dead-pid", state_path, False),
    )

    assert "PID: 999999" not in context
    assert "## Where we left off" not in context


def test_standalone_dead_pid_is_preserved_when_handoff_has_other_content(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "dead-pid-with-context"
    project.mkdir(parents=True)
    state_path = projects / "dead-pid-with-context" / "state.md"

    def dead_process(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", dead_process)
    _write_state(
        state_path,
        project,
        "dead-pid-with-context",
        "## Where we left off\n"
        "PID: 999999\n"
        "Investigate why the worker stopped before checkpointing.",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("dead-pid-with-context", state_path, False),
    )

    assert "PID: 999999" in context
    assert "Investigate why the worker stopped" in context


def test_alive_process_status_and_durable_pid_discussion_are_preserved(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "live-pid"
    project.mkdir(parents=True)
    state_path = projects / "live-pid" / "state.md"
    monkeypatch.setattr(os, "kill", lambda _pid, _signal: None)
    _write_state(
        state_path,
        project,
        "live-pid",
        "## Where we left off\n"
        "- FreeCAD GUI process PID 4242 is still listed as active.\n"
        "- Durable rule: never signal PID: 999999 without ownership proof.",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("live-pid", state_path, False),
    )

    assert "PID 4242 is still listed as active" in context
    assert "never signal PID: 999999" in context


def test_punctuation_only_bullets_are_not_useful_project_content(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "punctuation-only"
    project.mkdir(parents=True)
    state_path = projects / "punctuation-only" / "state.md"
    _write_state(
        state_path,
        project,
        "punctuation-only",
        "## Where we left off\n- ---\n- ...\n* !!!",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("punctuation-only", state_path, False),
    )

    assert "## Where we left off" not in context
    assert "Saved project state" not in context


def test_real_handoff_discussion_of_a_pid_is_preserved(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "pid-parser"
    project.mkdir(parents=True)
    state_path = projects / "pid-parser" / "state.md"
    _write_state(
        state_path,
        project,
        "pid-parser",
        "## Where we left off\n"
        "Investigate why the parser rejects PID 4242 tokens in imported logs.",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("pid-parser", state_path, False),
    )

    assert "PID 4242 tokens" in context


@pytest.mark.parametrize(
    "pid_text",
    ("0", "2147483648", "9" * 5000),
    ids=("zero", "above-platform-range", "overlong"),
)
def test_malformed_process_status_pid_is_noise_without_process_probe(
    monkeypatch,
    pid_text: str,
):
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("malformed PID must not reach os.kill")
        ),
    )
    line = f"- FreeCAD GUI process PID {pid_text} is still listed as active."

    assert session_start_project_state._state_line_is_dead_process_metadata(line)
    assert not session_start_project_state._state_line_is_dead_process_metadata(
        f"Investigate why imported logs mention PID {pid_text} tokens."
    )


@pytest.mark.parametrize(
    "error",
    (ValueError("invalid pid"), OverflowError("pid overflow"), OSError(errno.EINVAL, "bad pid")),
    ids=("value-error", "overflow-error", "os-error"),
)
def test_process_probe_errors_make_status_metadata_noise(monkeypatch, error: Exception):
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(error),
    )

    assert session_start_project_state._state_line_is_dead_process_metadata(
        "- FreeCAD GUI process PID 4242 is running."
    )


def test_overlong_standalone_pid_never_breaks_hook_output(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "overlong-pid"
    project.mkdir(parents=True)
    state_path = projects / "overlong-pid" / "state.md"
    pid_text = "9" * 5000
    _write_state(
        state_path,
        project,
        "overlong-pid",
        f"## Where we left off\nPID: {pid_text}",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("overlong-pid", state_path, False),
    )

    assert pid_text not in context
    assert "## Where we left off" not in context


def test_reparse_state_file_cannot_supply_a_trusted_handoff(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "reparse-project"
    project.mkdir(parents=True)
    state_path = projects / "reparse-project" / "state.md"
    _write_state(
        state_path,
        project,
        "reparse-project",
        "## Where we left off\nREPARSE_HANDOFF_MUST_NOT_APPEAR",
    )
    real_lstat = Path.lstat

    def reparse_lstat(path: Path, *args, **kwargs):
        metadata = real_lstat(path, *args, **kwargs)
        if path == state_path:
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("reparse-project", state_path, False),
    )

    assert "REPARSE_HANDOFF_MUST_NOT_APPEAR" not in context


def test_state_reader_rejects_windows_reparse_metadata_before_open(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "work" / "reparse-reader"
    project.mkdir(parents=True)
    state_path = tmp_path / "projects" / "reparse-reader" / "state.md"
    _write_state(
        state_path,
        project,
        "reparse-reader",
        "## Where we left off\nREPARSE_READER_MUST_NOT_APPEAR",
    )
    real_lstat = Path.lstat

    def reparse_lstat(path: Path, *args, **kwargs):
        metadata = real_lstat(path, *args, **kwargs)
        if path != state_path:
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            st_size=metadata.st_size,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_nlink=metadata.st_nlink,
            st_mtime_ns=metadata.st_mtime_ns,
        )

    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    assert session_start_project_state._read_state_ownership_body(state_path) is None


def test_state_reader_rejects_path_replaced_after_lstat(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "work" / "swap-reader"
    other = tmp_path / "work" / "other"
    project.mkdir(parents=True)
    other.mkdir(parents=True)
    state_path = tmp_path / "projects" / "swap-reader" / "state.md"
    replacement = state_path.with_name("replacement.md")
    _write_state(state_path, project, "swap-reader", "ORIGINAL_STATE")
    _write_state(replacement, other, "other", "REPLACEMENT_STATE")
    displaced = state_path.with_name("displaced.md")
    real_lstat = Path.lstat
    swapped = False

    def swapping_lstat(path: Path, *args, **kwargs):
        nonlocal swapped
        metadata = real_lstat(path, *args, **kwargs)
        if path == state_path and not swapped:
            swapped = True
            os.replace(state_path, displaced)
            os.replace(replacement, state_path)
        return metadata

    monkeypatch.setattr(Path, "lstat", swapping_lstat)

    assert session_start_project_state._read_state_ownership_body(state_path) is None
    assert swapped is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow behavior")
def test_state_reader_rejects_posix_symlink(tmp_path: Path):
    project = tmp_path / "work" / "symlink-reader"
    project.mkdir(parents=True)
    outside = tmp_path / "outside-state.md"
    _write_state(outside, project, "symlink-reader", "OUTSIDE_STATE")
    state_path = tmp_path / "projects" / "symlink-reader" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.symlink_to(outside)

    assert session_start_project_state._read_state_ownership_body(state_path) is None


def test_posix_state_open_uses_no_follow_and_validated_parent_descriptor(
    monkeypatch,
    tmp_path: Path,
):
    state_path = tmp_path / "state.md"
    no_follow = 0x20000
    calls: list[tuple[object, int, int | None]] = []
    monkeypatch.setattr(session_start_project_state.os, "name", "posix")
    monkeypatch.setattr(session_start_project_state.os, "O_NOFOLLOW", no_follow, raising=False)

    def fake_open(path, flags, *, dir_fd=None):
        calls.append((path, flags, dir_fd))
        return 17

    monkeypatch.setattr(session_start_project_state.os, "open", fake_open)
    bound = SimpleNamespace(descriptor=9)

    descriptor = session_start_project_state._open_state_descriptor(state_path, bound)

    assert descriptor == 17
    assert calls == [(state_path.name, calls[0][1], 9)]
    assert calls[0][1] & no_follow


def test_symlinked_requested_root_resolves_to_exact_owned_project(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "real-project"
    project.mkdir(parents=True)
    requested = tmp_path / "linked-project"
    try:
        requested.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    state_path = projects / "linked-project" / "state.md"
    _write_state(
        state_path,
        project,
        "linked-project",
        "## Where we left off\nSYMLINK_CANONICAL_HANDOFF",
    )

    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        requested,
        claimed=("linked-project", state_path, False),
    )

    assert "SYMLINK_CANONICAL_HANDOFF" in context


@pytest.mark.parametrize(
    ("root", "platform", "expected"),
    (
        (r"C:\projects\alpha", "win32", True),
        (r"\\server\share\alpha", "win32", True),
        ("/srv/projects/alpha", "linux", True),
        ("/srv/projects/alpha", "win32", False),
        (r"C:\projects\alpha", "linux", False),
    ),
)
def test_project_root_validation_uses_native_windows_and_posix_flavors(
    root: str,
    platform: str,
    expected: bool,
):
    assert session_start_project_state._is_native_absolute_root(root, platform) is expected


def test_bootstrap_without_freshness_metadata_is_not_injected(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "legacy-bootstrap"
    project.mkdir(parents=True)
    state_path = projects / "legacy-bootstrap" / "state.md"
    _write_state(state_path, project, "legacy-bootstrap", "")
    _write_bootstrap(
        state_path,
        project,
        "legacy-bootstrap",
        "MISSING_FRESHNESS_BOOTSTRAP",
        include_git_head=False,
    )

    context = _run_hook(monkeypatch, capsys, vault, project)

    assert "MISSING_FRESHNESS_BOOTSTRAP" not in context
    assert "Project bootstrap" not in context


@pytest.mark.parametrize(
    "variant",
    (
        "body-only",
        "fenced-body",
        "wrong-type",
        "duplicate-type",
        "unknown-key",
        "non-scalar-title",
        "duplicate-project-root",
    ),
)
def test_bootstrap_provenance_requires_one_strict_typed_frontmatter_mapping(
    monkeypatch,
    capsys,
    tmp_path: Path,
    variant: str,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "strict-bootstrap"
    project.mkdir(parents=True)
    state_path = projects / "strict-bootstrap" / "state.md"
    _write_state(state_path, project, "strict-bootstrap", "")
    _write_bootstrap(
        state_path,
        project,
        "strict-bootstrap",
        "STRICT_BOOTSTRAP_MUST_NOT_APPEAR",
    )
    bootstrap_path = state_path.with_name("bootstrap.md")
    text = bootstrap_path.read_text(encoding="utf-8")
    provenance = (
        'project_slug_json: "strict-bootstrap"\n'
        f"project_root_json: {json.dumps(str(project.resolve()))}\n"
        f"project_state_path_json: {json.dumps(str(state_path.resolve()))}\n"
        "git_head_json: null\n"
    )
    if variant == "body-only":
        text = f"# Bootstrap\n{provenance}\nSTRICT_BOOTSTRAP_MUST_NOT_APPEAR\n"
    elif variant == "fenced-body":
        text = (
            "---\ntype: bootstrap-context\n---\n"
            f"```yaml\n{provenance}```\n"
            "STRICT_BOOTSTRAP_MUST_NOT_APPEAR\n"
        )
    elif variant == "wrong-type":
        text = text.replace("type: bootstrap-context", "type: project-context")
    elif variant == "duplicate-type":
        text = text.replace(
            "type: bootstrap-context\n",
            "type: bootstrap-context\ntype: bootstrap-context\n",
        )
    elif variant == "unknown-key":
        text = text.replace("type: bootstrap-context\n", "type: bootstrap-context\nproject: strict-bootstrap\n")
    elif variant == "non-scalar-title":
        text = text.replace("type: bootstrap-context\n", "type: bootstrap-context\ntitle: [invalid]\n")
    else:
        text = text.replace(
            "project_root_json:",
            f"project_root_json: {json.dumps(str(project.resolve()))}\nproject_root_json:",
            1,
        )
    bootstrap_path.write_text(text, encoding="utf-8")

    context = _run_hook(monkeypatch, capsys, vault, project)

    assert "STRICT_BOOTSTRAP_MUST_NOT_APPEAR" not in context
    assert "Project bootstrap" not in context


@pytest.mark.parametrize("variant", ("slug-case", "root-dot", "state-dot"))
def test_bootstrap_provenance_requires_canonical_serialized_identity(
    monkeypatch,
    capsys,
    tmp_path: Path,
    variant: str,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "canonical-bootstrap"
    project.mkdir(parents=True)
    state_path = projects / "canonical-bootstrap" / "state.md"
    _write_state(state_path, project, "canonical-bootstrap", "")
    _write_bootstrap(
        state_path,
        project,
        "canonical-bootstrap",
        "NONCANONICAL_BOOTSTRAP_MUST_NOT_APPEAR",
    )
    bootstrap_path = state_path.with_name("bootstrap.md")
    text = bootstrap_path.read_text(encoding="utf-8")
    if variant == "slug-case":
        text = text.replace(
            'project_slug_json: "canonical-bootstrap"',
            'project_slug_json: "Canonical-Bootstrap"',
        )
    elif variant == "root-dot":
        noncanonical = f"{project.parent}{os.sep}.{os.sep}{project.name}"
        text = text.replace(
            f"project_root_json: {json.dumps(str(project.resolve()))}",
            f"project_root_json: {json.dumps(noncanonical)}",
        )
    else:
        noncanonical = f"{state_path.parent}{os.sep}.{os.sep}{state_path.name}"
        text = text.replace(
            f"project_state_path_json: {json.dumps(str(state_path.resolve()))}",
            f"project_state_path_json: {json.dumps(noncanonical)}",
        )
    bootstrap_path.write_text(text, encoding="utf-8")

    context = _run_hook(monkeypatch, capsys, vault, project)

    assert "NONCANONICAL_BOOTSTRAP_MUST_NOT_APPEAR" not in context
    assert "Project bootstrap" not in context


def test_explicit_no_head_bootstrap_is_current_for_a_non_git_project(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "non-git"
    project.mkdir(parents=True)
    state_path = projects / "non-git" / "state.md"
    _write_state(state_path, project, "non-git", "")
    _write_bootstrap(state_path, project, "non-git", "CURRENT_NO_HEAD_BOOTSTRAP")

    context = _run_hook(monkeypatch, capsys, vault, project)

    assert "Project bootstrap (UNTRUSTED project-derived data)" in context
    assert "CURRENT_NO_HEAD_BOOTSTRAP" in context


def test_project_inside_parent_git_repository_is_not_treated_as_non_git(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    repository = tmp_path / "work" / "repository"
    (repository / ".git").mkdir(parents=True)
    project = repository / "packages" / "nested-project"
    project.mkdir(parents=True)
    state_path = projects / "nested-project" / "state.md"
    _write_state(state_path, project, "nested-project", "")
    _write_bootstrap(
        state_path,
        project,
        "nested-project",
        "PARENT_REPOSITORY_BOOTSTRAP_MUST_NOT_APPEAR",
    )

    context = _run_hook(monkeypatch, capsys, vault, project)

    assert "PARENT_REPOSITORY_BOOTSTRAP_MUST_NOT_APPEAR" not in context
    assert "Project bootstrap" not in context


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


def _initialize_git_repository(path: Path, marker: str) -> str:
    path.mkdir(parents=True)
    _git("init", cwd=path)
    (path / "marker.txt").write_text(f"{marker}\n", encoding="utf-8")
    _git("add", "marker.txt", cwd=path)
    _git(
        "-c",
        "user.name=LLM Wiki Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        marker,
        cwd=path,
    )
    return _git("rev-parse", "HEAD", cwd=path)


def test_git_resolver_ignores_relative_path_and_project_local_executable(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "hostile-project"
    project.mkdir()
    executable = project / ("git.exe" if os.name == "nt" else "git")
    executable.write_bytes(b"hostile project-local executable")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join((".", "relative-bin", "")))

    assert session_start_project_state._resolve_git_executable() is None


def test_git_resolver_returns_absolute_regular_non_reparse_executable(
    monkeypatch,
    tmp_path: Path,
):
    executable_dir = tmp_path / "trusted-bin"
    executable_dir.mkdir()
    executable = executable_dir / ("git.exe" if os.name == "nt" else "git")
    executable.write_bytes(b"trusted executable")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(executable_dir.resolve()))

    resolved = session_start_project_state._resolve_git_executable()

    assert resolved == executable.resolve()
    assert resolved.is_absolute()


@pytest.mark.parametrize("stream_name", ("stdout", "stderr"))
def test_bounded_process_rejects_oversized_stream_without_unbounded_capture(
    tmp_path: Path,
    stream_name: str,
):
    script = (
        "import sys; "
        f"sys.{stream_name}.buffer.write(b'x' * 65536); "
        f"sys.{stream_name}.flush()"
    )

    result = session_start_project_state._run_bounded_process(
        [Path(sys.executable).resolve(), "-c", script],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=5,
        max_stdout_bytes=128,
        max_stderr_bytes=128,
    )

    assert result is None


def test_git_provenance_ignores_ambient_repository_a_to_b_overrides(
    monkeypatch,
    tmp_path: Path,
):
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    head_a = _initialize_git_repository(project_a, "project-a")
    head_b = _initialize_git_repository(project_b, "project-b")
    assert head_a != head_b
    overrides = {
        "GIT_DIR": str(project_b / ".git"),
        "GIT_WORK_TREE": str(project_b),
        "GIT_INDEX_FILE": str(project_b / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(project_b / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(project_b / ".git" / "objects"),
        "GIT_COMMON_DIR": str(project_b / ".git"),
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    assert session_start_project_state._current_project_git_head(project_a) == (
        True,
        head_a,
    )
    assert bootstrap_project._run_git(str(project_a), "rev-parse", "HEAD") == head_a


def test_git_provenance_clears_every_repository_router_but_preserves_safe_env(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    head = "a" * 40
    repository_routers = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ATTR_SOURCE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_VALUE_0",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_SUPER_PREFIX",
        "GIT_WORK_TREE",
    }
    for name in repository_routers:
        monkeypatch.setenv(name, "D:/ambient-project-b")
    monkeypatch.setenv("LLM_WIKI_SAFE_EXECUTABLE_ENV", "preserved")
    calls: list[tuple[list[str | Path], dict]] = []
    executable = Path(sys.executable).resolve()

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs)))
        stdout = str(project.resolve()) if "--show-toplevel" in command else head
        return session_start_project_state.BoundedProcessResult(
            0,
            stdout.encode(),
            b"",
        )

    monkeypatch.setattr(
        session_start_project_state,
        "_resolve_git_executable",
        lambda: executable,
    )
    monkeypatch.setattr(session_start_project_state, "_run_bounded_process", fake_run)

    assert session_start_project_state._current_project_git_head(project) == (True, head)
    assert bootstrap_project._run_git(str(project), "rev-parse", "HEAD") == head
    assert len(calls) == 3
    for command, kwargs in calls:
        assert Path(command[0]).is_absolute()
        environment = kwargs["env"]
        assert environment["LLM_WIKI_SAFE_EXECUTABLE_ENV"] == "preserved"
        assert all(Path(entry).is_absolute() for entry in environment["PATH"].split(os.pathsep))
        assert repository_routers.isdisjoint(environment)


def test_git_bootstrap_is_injected_only_at_its_recorded_head(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "git-project"
    project.mkdir(parents=True)
    _git("init", cwd=project)
    (project / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", "README.md", cwd=project)
    _git(
        "-c",
        "user.name=LLM Wiki Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "initial",
        cwd=project,
    )
    head = _git("rev-parse", "HEAD", cwd=project)
    state_path = projects / "git-project" / "state.md"
    _write_state(state_path, project, "git-project", "")
    _write_bootstrap(
        state_path,
        project,
        "git-project",
        "STALE_GIT_BOOTSTRAP",
        git_head="0" * len(head),
    )

    stale_context = _run_hook(monkeypatch, capsys, vault, project)
    assert "STALE_GIT_BOOTSTRAP" not in stale_context

    _write_bootstrap(
        state_path,
        project,
        "git-project",
        "CURRENT_GIT_BOOTSTRAP",
        git_head=head,
    )
    current_context = _run_hook(monkeypatch, capsys, vault, project)
    assert "CURRENT_GIT_BOOTSTRAP" in current_context


def test_bootstrap_head_check_is_bounded_and_runs_on_confirmed_root_only(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "bounded-git"
    (project / ".git").mkdir(parents=True)
    state_path = projects / "bounded-git" / "state.md"
    _write_state(state_path, project, "bounded-git", "")
    head = "a" * 40
    calls: list[tuple[list[str | Path], dict]] = []
    executable = Path(sys.executable).resolve()

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs)))
        if "--show-toplevel" in command:
            output = str(project.resolve())
        elif "HEAD^{commit}" in command:
            output = head
        else:
            output = ""
        return session_start_project_state.BoundedProcessResult(
            0,
            output.encode(),
            b"",
        )

    monkeypatch.setattr(
        session_start_project_state,
        "_resolve_git_executable",
        lambda: executable,
    )
    monkeypatch.setattr(session_start_project_state, "_run_bounded_process", fake_run)
    _write_bootstrap(
        state_path,
        project,
        "bounded-git",
        "BOUNDED_GIT_BOOTSTRAP",
        git_head=head,
    )
    calls.clear()

    context = _run_hook(monkeypatch, capsys, vault, project)

    assert "BOUNDED_GIT_BOOTSTRAP" in context
    assert len(calls) == 5
    assert all(Path(args[0]) == executable for args, _kwargs in calls)
    assert all(Path(kwargs["cwd"]).resolve() == project.resolve() for _args, kwargs in calls)
    assert all(0 < kwargs["timeout"] <= 10 for _args, kwargs in calls)
    assert all(
        kwargs["timeout"] <= 5
        for args, kwargs in calls
        if "rev-parse" in args
    )
    assert all(not kwargs.get("shell", False) for _args, kwargs in calls)


def test_standalone_main_shares_one_validated_snapshot_with_launcher_and_renderer(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    import memory_state

    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "snapshot-project"
    (project / ".git").mkdir(parents=True)
    state_path = projects / "snapshot-project" / "state.md"
    _write_state(
        state_path,
        project,
        "snapshot-project",
        "## Where we left off\nSHARED_SNAPSHOT_HANDOFF",
    )
    executable = Path(sys.executable).resolve()
    head = "a" * 40
    git_calls: list[list[str | Path]] = []
    git_resolutions = 0

    def resolve_git() -> Path:
        nonlocal git_resolutions
        git_resolutions += 1
        return executable

    def fake_git(command, **_kwargs):
        git_calls.append(list(command))
        output = str(project.resolve()) if "--show-toplevel" in command else head
        return session_start_project_state.BoundedProcessResult(
            0,
            output.encode(),
            b"",
        )

    monkeypatch.setattr(
        session_start_project_state,
        "_resolve_git_executable",
        resolve_git,
    )
    monkeypatch.setattr(
        session_start_project_state,
        "_run_bounded_process",
        fake_git,
    )
    _write_bootstrap(
        state_path,
        project,
        "snapshot-project",
        "SHARED_SNAPSHOT_BOOTSTRAP",
        git_head=head,
    )
    git_calls.clear()
    git_resolutions = 0
    state_reads = 0
    real_state_reader = session_start_project_state._read_trusted_state_body

    def counted_state_reader(*args, **kwargs):
        nonlocal state_reads
        state_reads += 1
        return real_state_reader(*args, **kwargs)

    monkeypatch.setattr(
        session_start_project_state,
        "_read_trusted_state_body",
        counted_state_reader,
    )
    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        lambda *_args, **_kwargs: ("snapshot-project", state_path, False),
    )
    monkeypatch.setattr(
        memory_state,
        "spawn_detached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("current bootstrap must not launch a worker")
        ),
    )
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(vault / "runtime"))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "project_dir": str(project),
                    "cwd": str(project),
                }
            )
        ),
    )

    assert session_start_project_state.main() == 0

    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "SHARED_SNAPSHOT_HANDOFF" in context
    assert "SHARED_SNAPSHOT_BOOTSTRAP" in context
    assert state_reads == 1
    assert git_resolutions == 1
    assert len(git_calls) == 5


def test_invalid_state_provenance_is_rejected_before_any_git_call(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "work" / "requested"
    other = tmp_path / "work" / "other"
    (project / ".git").mkdir(parents=True)
    other.mkdir(parents=True)
    state_path = projects / "requested" / "state.md"
    _write_state(state_path, other, "requested", "")
    _write_bootstrap(
        state_path,
        other,
        "requested",
        "CROSS_PROJECT_BOOTSTRAP_MUST_NOT_APPEAR",
        git_head="a" * 40,
    )

    def forbidden_git(*_args, **_kwargs):
        raise AssertionError("git must not run on an unconfirmed project root")

    monkeypatch.setattr(
        session_start_project_state,
        "_run_bounded_process",
        forbidden_git,
    )
    context = _run_hook(
        monkeypatch,
        capsys,
        vault,
        project,
        claimed=("requested", state_path, False),
    )

    assert "CROSS_PROJECT_BOOTSTRAP_MUST_NOT_APPEAR" not in context


def test_cached_state_body_cannot_bypass_bootstrap_identity_validation(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "work" / "cached-bootstrap"
    other = tmp_path / "work" / "other"
    project.mkdir(parents=True)
    other.mkdir(parents=True)
    state_path = tmp_path / "projects" / "cached-bootstrap" / "state.md"
    _write_state(state_path, project, "cached-bootstrap", "")
    _write_bootstrap(
        state_path,
        project,
        "cached-bootstrap",
        "CACHED_BOOTSTRAP_MUST_NOT_APPEAR",
    )
    forged = (
        "# Forged state\n"
        f"- Project root JSON: {json.dumps(str(other.resolve()))}\n"
        '- Runtime slug JSON: "cached-bootstrap"\n'
    )
    monkeypatch.setattr(
        session_start_project_state,
        "_read_trusted_state_body",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached validation must not reread state")
        ),
    )

    bootstrap = session_start_project_state._read_bootstrap_context(
        state_path,
        "cached-bootstrap",
        project,
        trusted_state_body=forged,
    )

    assert bootstrap == ""


def test_bootstrap_publication_records_explicit_no_head_provenance(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    project = tmp_path / "work" / "published-non-git"
    project.mkdir(parents=True)
    state_path = projects / "published-non-git" / "state.md"
    _write_state(state_path, project, "published-non-git", "")
    monkeypatch.setattr(bootstrap_project, "ROOT", vault)
    monkeypatch.setattr(bootstrap_project, "STATE_DIR", tmp_path / "runtime" / "run")
    monkeypatch.setattr(bootstrap_project, "PROJECTS_DIR", projects)
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd, **_kwargs: [])
    monkeypatch.setattr(bootstrap_project, "_extract_readme_summary", lambda _cwd: "Summary")
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_docs_structure", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args, **_kwargs: "")

    result = bootstrap_project.bootstrap(str(project), apply=True)

    assert result.startswith("Written:")
    serialized = state_path.with_name("bootstrap.md").read_text(encoding="utf-8")
    assert "git_head_json: null\n" in serialized
