"""Regression test: session_start_context strips machine-specific noise (Round 3 #I4).

Injected additionalContext MUST NOT include:
  - `Trigger: ...`  (hook metadata)
  - `Transcript: ...` (local filesystem paths)
  - `Project root: ...` (absolute paths, machine-specific)
  - Session-end header UUIDs (literal session IDs)

Useful signal must survive:
  - `# Session Memory Index` header
  - Wikilinks into knowledge/notes/
  - `Project slug: ...` (project identity, useful)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import bootstrap_project
import build_context
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "session_start_context.py"
SESSION_START_READER_MODULES = (
    "session_start_context",
    "session_start_project_state",
)


def _empty_effect_receipt(digest: str, generation: str = "a" * 64) -> dict:
    return {
        "version": 1,
        "daily_sha256": digest,
        "generation_id": generation,
        "journal_ids": [],
        "effects": [],
        "targets": [],
        "index": {"generation_id": generation, "entries": []},
    }


class _TrackingHookInput(io.StringIO):
    def __init__(self, value: str):
        super().__init__(value)
        self.read_calls = 0

    def read(self, size: int = -1) -> str:
        self.read_calls += 1
        return super().read(size)


@pytest.mark.parametrize(
    "payload",
    (
        '{"outer":{"duplicate":1,"duplicate":2}}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":"\\ud800"}',
    ),
    ids=("nested-duplicate", "nan", "infinity", "negative-infinity", "surrogate"),
)
def test_shared_json_object_decoder_rejects_non_strict_payloads(payload):
    import memory_state

    decoded, status = memory_state.read_json_object_bounded_with_status(
        io.StringIO(payload),
        max_bytes=4_096,
    )

    assert decoded is None
    assert status == "invalid"


def test_state_loader_quarantines_duplicate_keys(monkeypatch, tmp_path):
    import memory_state

    state_dir = tmp_path / "run"
    state_dir.mkdir()
    state_file = state_dir / "state.json"
    state_file.write_text(
        '{"compiled_daily_hashes":{},"compiled_daily_hashes":{"forged":"x"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")

    assert memory_state.load_state() == {}
    assert not state_file.exists()
    quarantined = list(state_dir.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert b'"forged"' in quarantined[0].read_bytes()


def _configure_manual_builder(monkeypatch, vault: Path) -> tuple[Path, Path, Path]:
    notes = vault / "knowledge" / "notes"
    daily = vault / "knowledge" / "daily"
    projects = vault / "knowledge" / "projects"
    monkeypatch.setattr(build_context, "ROOT", vault)
    monkeypatch.setattr(build_context, "KNOWLEDGE", notes)
    monkeypatch.setattr(build_context, "DAILY_DIR", daily)
    monkeypatch.setattr(build_context, "PROJECTS_DIR", projects)
    monkeypatch.setattr(build_context, "load_state", lambda: {})
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(vault / "runtime"))
    return notes, daily, projects


def _write_manual_builder_state(
    projects: Path,
    folder: str,
    alias: str,
    project_root: Path,
    handoff: str,
) -> Path:
    project_root.mkdir(parents=True, exist_ok=True)
    state_path = projects / folder / "state.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        f"# {alias} state\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        f"- Runtime slug JSON: {json.dumps(alias)}\n"
        f"## Where we left off\n- {handoff}\n",
        encoding="utf-8",
    )
    return state_path


def _write_manual_builder_page(
    notes: Path,
    filename: str,
    project: str,
    sentinel: str,
    *,
    project_root: Path | None = None,
    page_type: str = "pattern",
) -> None:
    notes.mkdir(parents=True, exist_ok=True)
    root_line = (
        f"project_root: {json.dumps(str(project_root.resolve()))}\n"
        if project_root is not None
        else ""
    )
    (notes / f"{filename}.md").write_text(
        "---\n"
        f"type: {page_type}\n"
        f"project: {project}\n"
        f"{root_line}"
        "status: active\n"
        "source_authority: opencode\n"
        "---\n"
        f"# {filename} knowledge\n"
        f"One-sentence summary: {sentinel}\n",
        encoding="utf-8",
    )


def _render_combined_identity_context(
    monkeypatch,
    tmp_path: Path,
    identity: str,
    *,
    slug: str,
    payload_size: int = 800,
    project_secondary: str | None = None,
) -> str:
    import session_start_context

    project_root = tmp_path / "active-project"
    project_root.mkdir()
    state_path = tmp_path / "projects" / slug / "state.md"
    state_path.parent.mkdir(parents=True)
    if project_secondary is None:
        trusted_state_parts = (
            identity,
            "## Where we left off\nCOMBINED_PRIORITY_HANDOFF_"
            + ("h" * payload_size),
            "# Authored state\nCOMBINED_SECONDARY_DETAIL_"
            + ("d" * payload_size),
        )
        bootstrap = "COMBINED_BOOTSTRAP_" + ("b" * payload_size)
    else:
        trusted_state_parts = (identity, project_secondary, "")
        bootstrap = ""
    snapshot = session_start_context.ProjectContextSnapshot(
        slug=slug,
        state_path=state_path,
        project_root=project_root.resolve(),
        trusted_state_body="trusted synthetic state",
        trusted_state_parts=trusted_state_parts,
        bootstrap=bootstrap,
    )
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _active: (slug, state_path),
    )
    monkeypatch.setattr(
        session_start_context,
        "_load_project_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", tmp_path / "missing-daily")
    monkeypatch.setattr(session_start_context, "_recent_daily_paths", lambda: [])
    monkeypatch.setattr(
        session_start_context,
        "trim_index",
        lambda _text: "INDEX_SECONDARY_" + ("i" * payload_size),
    )
    monkeypatch.setattr(
        session_start_context,
        "_daily_section",
        lambda _name, _record, _fallback, budget: session_start_context._bounded_block(
            "## Latest daily log: synthetic\n\n" + ("q" * payload_size),
            budget,
        ),
    )
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda *_args: "## Guard rails\n\n" + ("g" * payload_size),
    )
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\n"
        + ("m" * payload_size),
    )
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "## Advisory\n\n" + ("a" * payload_size),
    )
    monkeypatch.setattr(
        session_start_context,
        "last_log_entries",
        lambda _count: "LOG_SECONDARY_" + ("l" * payload_size),
    )
    return session_start_context.build_context(project_root)


def _combined_identity_at_marker_boundary(
    module,
    slug: str,
    *,
    extra_chars: int = 0,
) -> tuple[str, str, str]:
    root_prefix = '- Project root JSON: "/'
    root_suffix = '"'
    slug_line = f"- Runtime slug JSON: {json.dumps(slug)}"
    base_identity = f"{root_prefix}{root_suffix}\n{slug_line}"
    base_mandatory = "\n\n".join(
        ("## Current project state", f"**Project:** `{slug}`", base_identity)
    )
    base_context = (
        f"{module.CONTEXT_HEADING}\n\n{base_mandatory}\n\n"
        f"{module.SECTION_TRUNCATION_MARKER}\n"
    )
    filler_size = module.MAX_CONTEXT_CHARS - len(base_context) + extra_chars
    assert filler_size >= 0
    root_line = f'{root_prefix}{"x" * filler_size}{root_suffix}'
    return f"{root_line}\n{slug_line}", root_line, slug_line


@pytest.mark.parametrize("module_name", SESSION_START_READER_MODULES)
def test_session_start_reader_rejects_nested_lone_surrogate(module_name):
    module = __import__(module_name)
    payload = {
        "cwd": "D:/projects/example",
        "ignored": {"nested": [chr(0xD800)]},
    }

    assert module._read_hook_payload(io.StringIO(json.dumps(payload))) is None


@pytest.mark.parametrize("module_name", SESSION_START_READER_MODULES)
def test_session_start_reader_rejects_deep_json_nesting(module_name):
    depth = sys.getrecursionlimit() + 100
    payload = '{"ignored":' + "[" * depth + "null" + "]" * depth + "}"

    module = __import__(module_name)
    assert module._read_hook_payload(io.StringIO(payload)) is None


@pytest.mark.parametrize("module_name", SESSION_START_READER_MODULES)
def test_session_start_reader_enforces_input_limit_in_bytes(module_name):
    payload = json.dumps({"ignored": "é" * 32_000}, ensure_ascii=False)
    assert len(payload) < 64_000
    assert len(payload.encode("utf-8")) > 64_000

    module = __import__(module_name)
    assert module._read_hook_payload(io.StringIO(payload)) is None


@pytest.mark.parametrize("module_name", SESSION_START_READER_MODULES)
def test_session_start_reader_returns_before_reading_tty(module_name):
    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

        @staticmethod
        def read(*_args, **_kwargs):
            raise AssertionError("interactive stdin must not be read")

    module = __import__(module_name)
    assert module._read_hook_payload(TtyInput()) == {}


@pytest.mark.parametrize("module_name", SESSION_START_READER_MODULES)
def test_session_start_reader_preserves_normal_payload(module_name):
    payload = {
        "cwd": "D:/projects/example",
        "metadata": {"nested": ["value", 3, True]},
    }

    module = __import__(module_name)
    assert module._read_hook_payload(io.StringIO(json.dumps(payload))) == payload


def test_session_start_context_rejected_input_blocks_env_fallback_and_reads_once(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    import session_start_context

    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    stream = _TrackingHookInput("{not json")
    build_calls: list[tuple] = []
    daily_calls: list[bool] = []
    debug_calls: list[tuple] = []

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", runtime / "logs")
    monkeypatch.setattr(
        session_start_context,
        "build_context",
        lambda *args, **kwargs: build_calls.append((args, kwargs)) or "MUST_NOT_BUILD",
    )
    monkeypatch.setattr(
        session_start_context,
        "latest_daily",
        lambda: daily_calls.append(True) or None,
    )
    monkeypatch.setattr(
        session_start_context,
        "write_debug",
        lambda *args: debug_calls.append(args),
    )

    assert session_start_context.main() == 0
    assert stream.read_calls == 1
    assert build_calls == []
    assert daily_calls == []
    assert debug_calls == []
    assert not runtime.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"] == ""


def test_session_start_context_rejected_output_clears_stale_without_other_writes(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    output = tmp_path / "cache" / "session-context.md"
    output.parent.mkdir()
    output.write_text("PRIVATE_PROJECT_CONTEXT\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    stream = _TrackingHookInput("{not json")
    build_calls: list[tuple] = []
    daily_calls: list[bool] = []
    debug_calls: list[tuple] = []

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--output-file", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", runtime / "logs")
    monkeypatch.setattr(
        session_start_context,
        "build_context",
        lambda *args, **kwargs: build_calls.append((args, kwargs)) or "MUST_NOT_BUILD",
    )
    monkeypatch.setattr(
        session_start_context,
        "latest_daily",
        lambda: daily_calls.append(True) or None,
    )
    monkeypatch.setattr(
        session_start_context,
        "write_debug",
        lambda *args: debug_calls.append(args),
    )

    assert session_start_context.main() == 0
    assert stream.read_calls == 1
    assert output.read_text(encoding="utf-8") == ""
    assert build_calls == []
    assert daily_calls == []
    assert debug_calls == []
    assert not runtime.exists()


def test_session_start_context_explicit_directory_does_not_read_empty_stdin(
    tmp_path: Path,
):
    import session_start_context

    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    accepted_stream = _TrackingHookInput("")
    conflicting_stream = _TrackingHookInput("")

    accepted = session_start_context._active_project_directory(
        str(project),
        stream=accepted_stream,
        env={"CLAUDE_PROJECT_DIR": str(project)},
    )
    conflicting = session_start_context._active_project_directory(
        str(project),
        stream=conflicting_stream,
        env={"CLAUDE_PROJECT_DIR": str(other)},
    )

    assert accepted_stream.read_calls == 0
    assert accepted.root == project.resolve()
    assert accepted.signal_present is True
    assert conflicting_stream.read_calls == 0
    assert conflicting.root is None
    assert conflicting.signal_present is True


def test_project_state_rejected_input_blocks_env_fallback_and_side_effects(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    import session_start_project_state

    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    projects.mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    stream = _TrackingHookInput("{not json")
    claims: list[tuple] = []
    bootstrap_calls: list[tuple] = []
    state_path = projects / "project" / "state.md"

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(runtime))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        lambda *args: claims.append(args) or ("project", state_path, True),
    )
    monkeypatch.setattr(
        session_start_project_state,
        "_bootstrap_project_state",
        lambda *args: bootstrap_calls.append(args),
    )
    monkeypatch.setattr(
        session_start_project_state,
        "_build_context",
        lambda *_args: "PROJECT_CONTEXT_MUST_NOT_APPEAR",
    )

    assert session_start_project_state.main() == 0
    assert stream.read_calls == 1
    assert claims == []
    assert bootstrap_calls == []
    assert list(projects.iterdir()) == []
    assert not runtime.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"] == ""


def test_project_state_rejects_input_before_missing_projects_log(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    import session_start_project_state

    vault = tmp_path / "missing-vault"
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    project.mkdir()
    stream = _TrackingHookInput("{not json")

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(runtime))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdin", stream)

    assert session_start_project_state.main() == 0
    assert stream.read_calls == 1
    assert not vault.exists()
    assert not runtime.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"] == ""


def test_session_start_context_valid_empty_payload_keeps_env_fallback(tmp_path: Path):
    import session_start_context

    project = tmp_path / "project"
    project.mkdir()
    stream = _TrackingHookInput("{}")

    resolution = session_start_context._active_project_directory(
        None,
        stream=stream,
        env={"CLAUDE_PROJECT_DIR": str(project)},
    )

    assert stream.read_calls == 1
    assert resolution.root == project.resolve()
    assert resolution.signal_present is True


def test_project_state_valid_empty_payload_keeps_env_fallback(tmp_path: Path):
    import session_start_project_state

    project = tmp_path / "project"
    project.mkdir()
    stream = _TrackingHookInput("{}")
    payload = session_start_project_state._read_hook_payload(stream)

    assert payload == {}
    assert stream.read_calls == 1
    assert session_start_project_state._resolve_project_dir(
        payload,
        {"CLAUDE_PROJECT_DIR": str(project)},
    ) == project.resolve()


@pytest.fixture(scope="module")
def injected_context() -> str:
    # conftest.py bootstraps LLM_WIKI_ROOT and LLM_WIKI_STATE_ROOT in
    # os.environ; subprocess inherits. The script otherwise depends on
    # memory_state.ROOT (script-file-relative), which works regardless,
    # so this subprocess is safe even without env vars — but tests that
    # DO rely on env stay consistent.
    import os
    out = subprocess.check_output(
        [sys.executable, str(SCRIPT)],
        env=os.environ.copy(),
        input="{}",
        text=True,
    )
    d = json.loads(out)
    return d["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "forbidden",
    [
        "- Trigger:",
        "- Transcript:",
        "- Project root:",
        r"C:\Users\\",
    ],
)
def test_noise_stripped(injected_context: str, forbidden: str):
    assert forbidden not in injected_context, (
        f"injected context still contains forbidden fragment: {forbidden!r}"
    )


def test_no_session_uuid(injected_context: str):
    """Session-end headers should have their `| <uuid>` tail trimmed."""
    uuid_re = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    )
    assert not uuid_re.search(injected_context), (
        "UUID found in injected context — session-id strip regex regressed"
    )


def test_useful_signal_preserved(injected_context: str):
    """Context should still carry navigable links and index header."""
    # index-derived content
    assert "Session Memory Index" in injected_context
    # at least one wikilink into the knowledge tree
    assert "[[knowledge/notes/" in injected_context


def test_latest_daily_ignores_readme(monkeypatch, tmp_path):
    """Daily metadata must never displace a date-named daily log."""
    import session_start_context

    (tmp_path / "README.md").write_text("daily metadata", encoding="utf-8")
    expected = tmp_path / "2026-07-16.md"
    expected.write_text("## [12:00:00] session\n", encoding="utf-8")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", tmp_path)

    assert session_start_context.latest_daily() == expected


def test_daily_excerpt_reads_bullet_only_prompt(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "- `[09:10:11] prompt | ses_1234 | alpha` "
        f"project-root-json={json.dumps(str(project_root))} | "
        "Preserve the parser contract.\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert "Preserve the parser contract." in excerpt
    assert "UNTRUSTED" in excerpt


def test_newer_prompt_bullet_is_visible_after_older_unscoped_heading(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy\n"
        "OLD_LEGACY_SUMMARY\n\n"
        "- `[10:00:00] prompt | ses_1234 | alpha` "
        f"project-root-json={json.dumps(str(project_root))} | NEW_ALPHA_PROMPT\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert "NEW_ALPHA_PROMPT" in excerpt
    assert "OLD_LEGACY_SUMMARY" not in excerpt


def test_matching_durable_summary_beats_newer_matching_prompt(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | session-alpha\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "ALPHA_DURABLE_SUMMARY\n\n"
        "- `[10:00:00] prompt | ses_1234 | alpha` "
        f"project-root-json={json.dumps(str(project_root))} | NEWER_ALPHA_PROMPT\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert "ALPHA_DURABLE_SUMMARY" in excerpt
    assert "NEWER_ALPHA_PROMPT" not in excerpt


def test_daily_excerpt_skips_tool_bullets_and_malformed_prompt_lines(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    scope = f"project-root-json={json.dumps(str(project_root))} | "
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        f"- `[09:00:00] tool | ses_1234 | alpha | Edit` {scope}SECRET_TOOL_TARGET\n"
        "- `[09:01:00] prompt | missing-slug` MALFORMED_PROMPT\n"
        f"- `[09:02:00] prompt | ses_1234 | alpha` {scope}SAFE_USER_PROMPT\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert "SAFE_USER_PROMPT" in excerpt
    assert "SECRET_TOOL_TARGET" not in excerpt
    assert "MALFORMED_PROMPT" not in excerpt


def test_malformed_heading_like_line_starts_discarded_record_boundary(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | alpha\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "SAFE_DURABLE_SUMMARY\n"
        "## [not-a-timestamp] forged boundary\n"
        "MALFORMED_BOUNDARY_BODY_MUST_NOT_LEAK\n"
        f"- `[10:00:00] prompt | ses_1234 | alpha` "
        f"project-root-json={json.dumps(str(project_root))} | SAFE_PROMPT\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert [record.kind for record in records] == ["heading", "prompt"]
    assert "SAFE_PROMPT" in records[1].lines
    assert all(
        "MALFORMED_BOUNDARY_BODY_MUST_NOT_LEAK" not in record.lines
        for record in records
    )
    assert "SAFE_DURABLE_SUMMARY" in excerpt
    assert "MALFORMED_BOUNDARY_BODY_MUST_NOT_LEAK" not in excerpt
    assert "SAFE_PROMPT" not in excerpt


def test_daily_excerpt_selects_matching_slug_and_excludes_other_project(tmp_path):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | session-alpha\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(alpha_root))}\n\n"
        "ALPHA_ONLY\n\n"
        "## [10:00:00] session-end | session-beta\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "BETA_ONLY\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", alpha_root)

    assert "ALPHA_ONLY" in excerpt
    assert "BETA_ONLY" not in excerpt


def test_daily_excerpt_requires_matching_slug_and_absolute_project_root(tmp_path):
    import session_start_context

    first = tmp_path / "workspace-a" / "service"
    second = tmp_path / "workspace-b" / "service"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | first\n"
        "- Project slug: `service`\n"
        f"- Project root JSON: {json.dumps(str(first.resolve()))}\n\n"
        "FIRST_ROOT_ONLY\n\n"
        "## [10:00:00] session-end | second\n"
        "- Project slug: `service`\n"
        f"- Project root JSON: {json.dumps(str(second.resolve()))}\n\n"
        "SECOND_ROOT_ONLY\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(
        daily,
        "service",
        first.resolve(),
    )

    assert "FIRST_ROOT_ONLY" in excerpt
    assert "SECOND_ROOT_ONLY" not in excerpt


@pytest.mark.parametrize(
    "scope_lines",
    (
        '- Project slug: `service`\n',
        '- Project root JSON: "D:/service"\n',
        '- Project root JSON: "unterminated\n- Project slug: `service`\n',
        '- Project slug: `service`\n- Project root JSON: "D:/one"\n'
        '- Project root JSON: "D:/two"\n',
    ),
)
def test_incomplete_malformed_or_contradictory_daily_scope_is_invalid(
    tmp_path, scope_lines
):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | unsafe\n"
        f"{scope_lines}\n"
        "UNSAFE_SCOPE_CONTENT\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )

    assert records == []


def test_root_scoped_compact_prompt_matches_only_its_project(tmp_path):
    import session_start_context

    project = tmp_path / "workspace" / "service"
    other = tmp_path / "other" / "service"
    project.mkdir(parents=True)
    other.mkdir(parents=True)
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "- `[09:10:11] prompt | ses_1234 | service` "
        f"project-root-json={json.dumps(str(project.resolve()))} | ROOTED_PROMPT\n",
        encoding="utf-8",
    )

    assert "ROOTED_PROMPT" in session_start_context.daily_excerpt(
        daily,
        "service",
        project.resolve(),
    )
    assert "ROOTED_PROMPT" not in session_start_context.daily_excerpt(
        daily,
        "service",
        other.resolve(),
    )


def test_daily_excerpt_without_active_slug_hides_explicit_project_records(tmp_path):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | session-alpha\n"
        "- Project slug: `alpha`\n\n"
        "ALPHA_PRIVATE_CONTEXT\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, None)

    assert "ALPHA_PRIVATE_CONTEXT" not in excerpt
    assert "no eligible" in excerpt


@pytest.mark.parametrize(
    "active_slug",
    [
        pytest.param(None, id="no-active-project"),
        pytest.param("alpha", id="active-unrelated-project"),
    ],
)
def test_invalid_explicit_slugs_are_skipped_instead_of_treated_as_legacy(
    tmp_path, active_slug
):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [08:00:00] session-end | legacy-session\n"
        "LEGACY_SAFE_FALLBACK\n\n"
        "## [09:00:00] session-end | corrupt-heading\n"
        "- Project slug: `beta|corrupt`\n\n"
        "INVALID_HEADING_MUST_NOT_LEAK\n\n"
        "- `[10:00:00] prompt | ses_1234 | beta|corrupt` "
        "INVALID_BULLET_MUST_NOT_LEAK\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, active_slug)

    assert len(records) == 1
    assert records[0].slug is None
    if active_slug is None:
        assert "LEGACY_SAFE_FALLBACK" in excerpt
    else:
        assert "LEGACY_SAFE_FALLBACK" not in excerpt
    assert "INVALID_HEADING_MUST_NOT_LEAK" not in excerpt
    assert "INVALID_BULLET_MUST_NOT_LEAK" not in excerpt


@pytest.mark.parametrize(
    "active_slug",
    [
        pytest.param(None, id="no-active-project"),
        pytest.param("alpha", id="active-unrelated-project"),
    ],
)
def test_oversized_explicit_slugs_are_skipped_before_bounded_parsing(
    tmp_path, active_slug
):
    import session_start_context

    oversized_slug = "beta-" + "x" * session_start_context.DAILY_RECORD_LINE_MAX
    heading_metadata = f"- Project slug: `{oversized_slug}`"
    compact_prompt = (
        f"- `[10:00:00] prompt | ses_1234 | {oversized_slug}` "
        "OVERSIZED_BULLET_MUST_NOT_LEAK"
    )
    assert len(heading_metadata) > session_start_context.DAILY_RECORD_LINE_MAX
    assert len(compact_prompt) > session_start_context.DAILY_RECORD_LINE_MAX

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [08:00:00] session-end | legacy-session\n"
        "GENUINE_LEGACY_FALLBACK\n\n"
        "## [09:00:00] session-end | oversized-heading\n"
        f"{heading_metadata}\n\n"
        "OVERSIZED_HEADING_MUST_NOT_LEAK\n\n"
        f"{compact_prompt}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, active_slug)

    assert len(records) == 1
    assert records[0].slug is None
    if active_slug is None:
        assert "GENUINE_LEGACY_FALLBACK" in excerpt
    else:
        assert "GENUINE_LEGACY_FALLBACK" not in excerpt
    assert "OVERSIZED_HEADING_MUST_NOT_LEAK" not in excerpt
    assert "OVERSIZED_BULLET_MUST_NOT_LEAK" not in excerpt


@pytest.mark.parametrize(
    "active_slug",
    [
        pytest.param(None, id="no-active-project"),
        pytest.param("alpha", id="active-unrelated-project"),
    ],
)
def test_leading_whitespace_cannot_hide_explicit_project_slug(tmp_path, active_slug):
    import session_start_context

    beta_root = (tmp_path / "beta").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [08:00:00] session-end | legacy-session\n"
        "GENUINE_LEGACY_FALLBACK\n\n"
        "## [09:00:00] session-end | beta-session\n"
        f"{' ' * 129}- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "INDENTED_BETA_CONTENT_MUST_NOT_LEAK\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, active_slug)

    assert len(records) == 2
    assert records[1].slug == "beta"
    if active_slug is None:
        assert "GENUINE_LEGACY_FALLBACK" in excerpt
    else:
        assert "GENUINE_LEGACY_FALLBACK" not in excerpt
    assert "INDENTED_BETA_CONTENT_MUST_NOT_LEAK" not in excerpt


@pytest.mark.parametrize(
    "active_slug",
    [
        pytest.param(None, id="no-active-project"),
        pytest.param("alpha", id="active-unrelated-project"),
    ],
)
def test_oversized_nonmetadata_line_invalidates_entire_heading_record(
    tmp_path, active_slug
):
    import session_start_context

    oversized_line = "x" * (session_start_context.DAILY_RECORD_LINE_MAX + 1)
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [08:00:00] session-end | legacy-session\n"
        "GENUINE_LEGACY_FALLBACK\n\n"
        "## [09:00:00] session-end | oversized-unscoped\n"
        f"{oversized_line}\n"
        "OVERSIZED_UNSCOPED_CONTENT_MUST_NOT_LEAK\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, active_slug)

    assert len(records) == 1
    assert records[0].slug is None
    if active_slug is None:
        assert "GENUINE_LEGACY_FALLBACK" in excerpt
    else:
        assert "GENUINE_LEGACY_FALLBACK" not in excerpt
    assert "OVERSIZED_UNSCOPED_CONTENT_MUST_NOT_LEAK" not in excerpt


@pytest.mark.parametrize(
    ("kind", "suffix", "wrapper"),
    (
        ("prompt", "", "{}"),
        ("tool", " | Edit", "{}"),
        ("prompt", "", "<analysis>{}</analysis>"),
        ("tool", " | Edit", "<summary>{}</summary>"),
    ),
)
def test_oversized_compact_boundary_does_not_invalidate_prior_heading(
    tmp_path, kind, suffix, wrapper
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    oversized_compact = (
        f"- `[10:00:00] {kind} | ses_1234 | alpha{suffix}` " + "x" * 5000
    )
    wrapped_compact = wrapper.format(oversized_compact)
    assert len(wrapped_compact) > session_start_context.DAILY_RECORD_LINE_MAX

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | durable-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "DURABLE_ALPHA_SUMMARY\n"
        f"{wrapped_compact}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert len(records) == 1
    assert records[0].kind == "heading"
    assert records[0].slug == "alpha"
    assert records[0].source_position == 0
    assert "DURABLE_ALPHA_SUMMARY" in excerpt


@pytest.mark.parametrize(
    "wrapper",
    ("{}", "<summary>{}</summary>"),
)
def test_oversized_heading_boundary_does_not_invalidate_prior_heading(
    tmp_path, wrapper
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    oversized_heading = "## [10:00:00] session-end | " + "x" * 5000
    wrapped_heading = wrapper.format(oversized_heading)
    assert len(wrapped_heading) > session_start_context.DAILY_RECORD_LINE_MAX

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | durable-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "DURABLE_ALPHA_SUMMARY\n"
        f"{wrapped_heading}\n"
        "OVERSIZED_HEADING_BODY_MUST_BE_SKIPPED\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert len(records) == 1
    assert records[0].slug == "alpha"
    assert "DURABLE_ALPHA_SUMMARY" in excerpt
    assert "OVERSIZED_HEADING_BODY_MUST_BE_SKIPPED" not in excerpt


def test_daily_excerpt_falls_back_to_legacy_unscoped_heading(tmp_path):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy-session\n"
        "LEGACY_UNSCOPED_SUMMARY\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(
        daily,
        "alpha",
        (tmp_path / "alpha").resolve(),
    )

    assert "LEGACY_UNSCOPED_SUMMARY" in excerpt


def test_no_pipe_deferred_precompact_legacy_reaches_parser_and_context(tmp_path):
    import session_start_context

    heading = "## [20:22:34] deferred-pre-compact"
    body = "DURABLE_NO_PIPE_PRECOMPACT_BODY"
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(f"{heading}\n{body}\n", encoding="utf-8")

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(
        daily,
        "alpha",
        (tmp_path / "alpha").resolve(),
    )

    assert session_start_context.DAILY_HEADING_RE.fullmatch(heading) is None
    assert len(records) == 1
    assert records[0].slug is None
    assert records[0].project_root is None
    assert records[0].lines[0] == heading
    assert body in excerpt


def test_no_pipe_legacy_event_length_is_bounded():
    import session_start_context

    accepted_event = "e" * 128
    rejected_event = "e" * 129
    text = (
        f"## [20:22:34] {accepted_event}\n"
        "MAX_LENGTH_LEGACY_BODY\n"
        f"## [20:22:35] {rejected_event}\n"
        "OVERSIZED_EVENT_BODY_MUST_BE_SKIPPED\n"
    )

    records = session_start_context.parse_daily_records(text)

    assert len(records) == 1
    assert records[0].lines[0] == f"## [20:22:34] {accepted_event}"
    assert "MAX_LENGTH_LEGACY_BODY" in records[0].lines
    assert all(
        "OVERSIZED_EVENT_BODY_MUST_BE_SKIPPED" not in record.lines
        for record in records
    )


def test_scope_bearing_no_pipe_beta_cannot_be_alpha_legacy_fallback(tmp_path):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [20:22:34] deferred-pre-compact\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "BETA_SCOPE_MUST_NOT_BECOME_ALPHA_LEGACY\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, "alpha", alpha_root)

    assert records == []
    assert "BETA_SCOPE_MUST_NOT_BECOME_ALPHA_LEGACY" not in excerpt


def test_daily_excerpt_prefers_matching_scoped_heading_over_legacy(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | scoped-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "SCOPED_ALPHA_SUMMARY\n\n"
        "## [10:00:00] session-end | newer-legacy-session\n"
        "NEWER_LEGACY_SUMMARY\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert "SCOPED_ALPHA_SUMMARY" in excerpt
    assert "NEWER_LEGACY_SUMMARY" not in excerpt


def test_daily_excerpt_legacy_fallback_excludes_explicit_other_project(tmp_path):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy-session\n"
        "SAFE_LEGACY_FALLBACK\n\n"
        "## [10:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "EXPLICIT_BETA_SUMMARY\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", alpha_root)

    assert "SAFE_LEGACY_FALLBACK" in excerpt
    assert "EXPLICIT_BETA_SUMMARY" not in excerpt


def test_markerless_scoped_headings_preserve_order_and_select_newest_match(
    tmp_path,
):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | alpha-older\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(alpha_root))}\n\n"
        "OLDER_ALPHA_SUMMARY\n"
        "## [10:00:00] session-end | alpha-newer\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(alpha_root))}\n\n"
        "NEWER_ALPHA_SUMMARY\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, "alpha", alpha_root)

    assert [record.slug for record in records] == ["alpha", "alpha"]
    assert [record.order for record in records] == [0, 1]
    assert [record.timestamp for record in records] == ["09:00:00", "10:00:00"]
    assert "OLDER_ALPHA_SUMMARY" in records[0].lines
    assert "NEWER_ALPHA_SUMMARY" in records[1].lines
    assert "NEWER_ALPHA_SUMMARY" in excerpt
    assert "OLDER_ALPHA_SUMMARY" not in excerpt


@pytest.mark.parametrize(
    ("header", "body"),
    (
        pytest.param(
            "## [09:01:00] deferred-pre-compact",
            "LEGACY_NO_PIPE_SUMMARY",
            id="no-pipe",
        ),
        pytest.param(
            "## [09:01:00] session-end | legacy-session",
            "LEGACY_PIPE_SUMMARY",
            id="pipe",
        ),
    ),
)
def test_markerless_unscoped_heading_after_scoped_heading_is_preserved(
    tmp_path,
    header,
    body,
):
    import session_start_context

    beta_root = (tmp_path / "beta").resolve()
    text = (
        "## [09:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "BETA_SCOPED_SUMMARY\n"
        f"{header}\n"
        f"{body}\n"
    )

    records = session_start_context.parse_daily_records(text)

    assert [record.slug for record in records] == ["beta", None]
    assert [record.order for record in records] == [0, 1]
    assert body in records[1].lines


@pytest.mark.parametrize(
    ("kind", "detail"),
    (
        pytest.param("prompt", "", id="prompt"),
        pytest.param("tool", " | Edit", id="tool"),
    ),
)
def test_markerless_compact_record_after_scoped_heading_is_preserved(
    tmp_path,
    kind,
    detail,
):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    text = (
        "## [09:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "BETA_SCOPED_SUMMARY\n"
        f"- `[09:01:00] {kind} | alpha-session | alpha{detail}` "
        f"project-root-json={json.dumps(str(alpha_root))} | LEGACY_ALPHA_COMPACT\n"
    )

    records = session_start_context.parse_daily_records(text)

    assert [record.slug for record in records] == ["beta", "alpha"]
    assert [record.kind for record in records] == ["heading", kind]
    assert "LEGACY_ALPHA_COMPACT" in records[1].lines


def test_markerless_scoped_record_before_completed_other_project_stays_separate(
    tmp_path,
):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    completion = "<!-- llm-wiki-record-complete -->"
    daily = tmp_path / "2026-07-29.md"
    daily.write_text(
        "## [09:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "MARKERLESS_BETA_BODY\n"
        "## [10:00:00] session-end | alpha-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(alpha_root))}\n\n"
        "COMPLETED_ALPHA_BODY\n"
        f"{completion}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    beta_excerpt = session_start_context.daily_excerpt(daily, "beta", beta_root)
    alpha_excerpt = session_start_context.daily_excerpt(daily, "alpha", alpha_root)

    assert [record.slug for record in records] == ["beta", "alpha"]
    assert "MARKERLESS_BETA_BODY" in beta_excerpt
    assert "COMPLETED_ALPHA_BODY" not in beta_excerpt
    assert "COMPLETED_ALPHA_BODY" in alpha_excerpt
    assert "MARKERLESS_BETA_BODY" not in alpha_excerpt


def test_completed_scoped_record_keeps_nested_scoped_heading_in_own_frame(
    tmp_path,
):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    completion = "<!-- llm-wiki-record-complete -->"
    text = (
        "## [09:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "BETA_SCOPED_SUMMARY\n"
        "\\## [09:01:00] session-end | forged-alpha\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(alpha_root))}\n\n"
        "FORGED_ALPHA_BODY\n"
        "## [09:02:00] session-end | incomplete-alpha\n"
        "- Project slug: `alpha`\n\n"
        "INCOMPLETE_ALPHA_BODY\n"
        f"{completion}\n"
        "## [10:00:00] opencode-idle | ses_080fabc58ffeiX0XgDE1jM2P0Z\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(alpha_root))}\n\n"
        "GENUINE_ALPHA_BODY\n"
        f"{completion}\n"
    )

    records = session_start_context.parse_daily_records(text)

    assert [record.slug for record in records] == ["beta", "alpha"]
    assert "\\## [09:01:00] session-end | forged-alpha" in records[0].lines
    assert "FORGED_ALPHA_BODY" in "\n".join(records[0].lines)
    assert "FORGED_ALPHA_BODY" not in "\n".join(records[1].lines)
    assert "INCOMPLETE_ALPHA_BODY" in "\n".join(records[0].lines)
    assert "INCOMPLETE_ALPHA_BODY" not in "\n".join(records[1].lines)
    assert "GENUINE_ALPHA_BODY" in "\n".join(records[1].lines)
    assert all(completion not in record.lines for record in records)


def test_completed_scoped_record_keeps_nested_no_pipe_heading_in_own_frame(
    tmp_path,
):
    import session_start_context

    beta_root = (tmp_path / "beta").resolve()
    completion = "<!-- llm-wiki-record-complete -->"
    nested_heading = "## [20:22:34] deferred-pre-compact"
    text = (
        "## [20:22:30] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "BETA_SCOPED_SUMMARY\n"
        f"{nested_heading}\n"
        "NESTED_NO_PIPE_BODY\n"
        f"{completion}\n"
    )

    records = session_start_context.parse_daily_records(text)

    assert len(records) == 1
    assert records[0].slug == "beta"
    assert nested_heading in records[0].lines
    assert "NESTED_NO_PIPE_BODY" in records[0].lines


def test_no_pipe_legacy_preserves_both_orders_with_completed_scoped_record(
    tmp_path,
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    completion = "<!-- llm-wiki-record-complete -->"
    legacy = (
        "## [20:22:34] deferred-pre-compact\n"
        "DURABLE_NO_PIPE_LEGACY_BODY\n"
    )
    scoped = (
        "## [20:23:00] session-end | scoped-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "COMPLETED_SCOPED_BODY\n"
        f"{completion}\n"
    )

    for text, expected_slugs in (
        (legacy + scoped, [None, "alpha"]),
        (scoped + legacy, ["alpha", None]),
    ):
        records = session_start_context.parse_daily_records(text)

        assert [record.slug for record in records] == expected_slugs
        assert [record.order for record in records] == [0, 1]
        assert sum(
            "DURABLE_NO_PIPE_LEGACY_BODY" in record.lines
            for record in records
        ) == 1
        assert sum("COMPLETED_SCOPED_BODY" in record.lines for record in records) == 1


def test_markerless_legacy_and_scoped_headings_preserve_both_orders(
    tmp_path,
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    legacy = (
        "## [00:57:04] session | fixture-0413-a\n"
        "LEGACY_FIXTURE_BODY\n"
    )
    scoped = (
        "## [10:40:42] session-end | reg-vault\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "SCOPED_FIXTURE_BODY\n"
    )
    for text, expected_slugs in (
        (legacy + scoped, [None, "alpha"]),
        (scoped + legacy, ["alpha", None]),
    ):
        records = session_start_context.parse_daily_records(text)

        assert [record.slug for record in records] == expected_slugs
        assert [record.order for record in records] == [0, 1]
        assert sum("LEGACY_FIXTURE_BODY" in record.lines for record in records) == 1
        assert sum("SCOPED_FIXTURE_BODY" in record.lines for record in records) == 1


def test_completed_scoped_record_can_be_followed_by_normal_boundaries(tmp_path):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    completion = "<!-- llm-wiki-record-complete -->"
    text = (
        "## [09:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "COMPLETED_BETA_BODY\n"
        f"{completion}\n"
        "## [09:01:00] session | legacy-session\n"
        "FOLLOWING_LEGACY_BODY\n"
        "- `[09:02:00] prompt | alpha-session | alpha` "
        f"project-root-json={json.dumps(str(alpha_root))} | FOLLOWING_ALPHA_PROMPT\n"
    )

    records = session_start_context.parse_daily_records(text)

    assert [record.slug for record in records] == ["beta", None, "alpha"]
    assert [record.order for record in records] == [0, 1, 2]
    assert "FOLLOWING_ALPHA_PROMPT" in records[2].lines


def test_markerless_cross_project_scoped_headings_are_separately_selectable(
    tmp_path,
):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(str(beta_root))}\n\n"
        "BETA_SCOPED_SUMMARY\n"
        "## [09:01:00] session-end | alpha-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(alpha_root))}\n\n"
        "ALPHA_SCOPED_SUMMARY\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    beta_excerpt = session_start_context.daily_excerpt(daily, "beta", beta_root)
    alpha_excerpt = session_start_context.daily_excerpt(daily, "alpha", alpha_root)

    assert [record.slug for record in records] == ["beta", "alpha"]
    assert [record.order for record in records] == [0, 1]
    assert "BETA_SCOPED_SUMMARY" in beta_excerpt
    assert "ALPHA_SCOPED_SUMMARY" not in beta_excerpt
    assert "ALPHA_SCOPED_SUMMARY" in alpha_excerpt
    assert "BETA_SCOPED_SUMMARY" not in alpha_excerpt


def test_daily_excerpt_skips_metadata_only_heading(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | useful-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "OLDER_SUBSTANTIVE_SUMMARY\n\n"
        "## [10:00:00] deferred-session-end | metadata-session\n"
        "- Trigger: `deferred`\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n"
        "- Transcript: `session.jsonl`\n"
        "- Tier: `minor`\n"
        "- Source session: `metadata-session`\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert "OLDER_SUBSTANTIVE_SUMMARY" in excerpt
    assert "metadata-session" not in excerpt


def test_direct_flush_marker_is_removed_across_render_parse_and_excerpt(tmp_path):
    import flush_memory
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    marker = flush_memory.direct_flush_marker(
        "session-1",
        "session-end",
        "2026-07-29T12:00:00+00:00",
        "alpha",
        str(project_root),
    )
    day, block = flush_memory.render_flush_block(
        "minor",
        "DIRECT_FLUSH_SUMMARY",
        event="session-end",
        session_id="session-1",
        trigger="hook",
        project_slug="alpha",
        project_root=str(project_root),
        occurred_at="2026-07-29T12:00:00+00:00",
        idempotency_marker=marker,
    )
    daily = tmp_path / f"{day}.md"
    daily.write_text(block, encoding="utf-8")

    records = session_start_context.parse_daily_records(block)
    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert len(records) == 1
    assert records[0].meaningful is True
    assert marker not in records[0].lines
    assert "DIRECT_FLUSH_SUMMARY" in excerpt
    assert marker not in excerpt


@pytest.mark.parametrize("marker_kind", ("queue-task", "direct-flush", "capture"))
def test_canonical_daily_idempotency_marker_is_not_meaningful(
    marker_kind,
    tmp_path,
):
    import session_start_context

    marker = f"<!-- llm-wiki-{marker_kind}: {'a' * 64} -->"
    daily = tmp_path / f"canonical-{marker_kind}.md"
    daily.write_text(
        "## [09:00:00] session-end | useful-session\n"
        "OLDER_SUBSTANTIVE_SUMMARY\n\n"
        "## [10:00:00] session-end | marker-only\n"
        f"{marker}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily)

    assert len(records) == 2
    assert records[1].meaningful is False
    assert marker not in records[1].lines
    assert "OLDER_SUBSTANTIVE_SUMMARY" in excerpt
    assert marker not in excerpt


@pytest.mark.parametrize("marker_kind", ("queue-task", "direct-flush", "capture"))
def test_malformed_daily_idempotency_marker_remains_untrusted_content(
    marker_kind,
    tmp_path,
):
    import session_start_context

    marker = f"<!-- llm-wiki-{marker_kind}: short -->"
    daily = tmp_path / f"malformed-{marker_kind}.md"
    daily.write_text(
        "## [10:00:00] session-end | malformed-marker\n"
        f"{marker}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily)

    assert len(records) == 1
    assert records[0].meaningful is True
    assert marker in records[0].lines
    assert "UNTRUSTED" in excerpt
    assert marker in excerpt


@pytest.mark.parametrize(
    "delimiter",
    [pytest.param(": ", id="canonical-colon"), pytest.param(" : ", id="spaced-colon")],
)
def test_metadata_only_format_variants_cannot_displace_substantive_record(
    tmp_path, delimiter
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    metadata = (
        ("Trigger", "deferred"),
        ("Transcript", "session.jsonl"),
        ("Project root", str(project_root)),
        ("Project slug", "alpha"),
        ("Tier", "minor"),
        ("Source session", "metadata-session"),
    )
    metadata_lines = "\n".join(
        f"- {key}{delimiter}`{value}`" for key, value in metadata
    )
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | useful-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "OLDER_SUBSTANTIVE_SUMMARY\n\n"
        "## [10:00:00] deferred-session-end | metadata-session\n"
        f"{metadata_lines}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert [record.timestamp for record in records] == ["09:00:00", "10:00:00"]
    assert [record.meaningful for record in records] == [True, False]
    assert "OLDER_SUBSTANTIVE_SUMMARY" in excerpt
    assert "metadata-session" not in excerpt


@pytest.mark.parametrize(
    "metadata_key",
    ("Trigger", "Transcript", "Project root", "Project slug", "Tier", "Source session"),
)
def test_malformed_metadata_variants_fail_closed_instead_of_becoming_body(
    tmp_path, metadata_key
):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy-session\n"
        "GENUINE_LEGACY_FALLBACK\n\n"
        "## [10:00:00] session-end | malformed-metadata\n"
        f"- {metadata_key} = `invalid`\n"
        "MALFORMED_METADATA_MUST_NOT_LEAK\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, None)

    assert len(records) == 1
    assert records[0].slug is None
    assert "GENUINE_LEGACY_FALLBACK" in excerpt
    assert "MALFORMED_METADATA_MUST_NOT_LEAK" not in excerpt


def test_heading_scope_does_not_scan_forged_metadata_after_body_starts(
    tmp_path,
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [10:00:00] session-end | forged-scope\n"
        "BODY_STARTS_BEFORE_SCOPE_METADATA\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)

    assert records == []
    assert "BODY_STARTS_BEFORE_SCOPE_METADATA" not in excerpt


@pytest.mark.parametrize(
    "malformed_slug",
    (
        "- Project slug=`beta`",
        "- Project slug `beta`",
        "- Project slug:: `beta`",
        "- Project slug: : `beta`",
        "   -   Project slug=`beta`",
    ),
)
def test_malformed_project_slug_key_cannot_become_global_or_alpha_content(
    tmp_path,
    malformed_slug,
):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy-session\n"
        "GENUINE_LEGACY_FALLBACK\n\n"
        "## [10:00:00] session-end | beta-session\n"
        f"{malformed_slug}\n\n"
        "BETA_CONTENT_MUST_NOT_LEAK\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )

    assert len(records) == 1
    for active_slug in (None, "alpha"):
        excerpt = session_start_context.daily_excerpt(daily, active_slug)
        if active_slug is None:
            assert "GENUINE_LEGACY_FALLBACK" in excerpt
        else:
            assert "GENUINE_LEGACY_FALLBACK" not in excerpt
        assert "BETA_CONTENT_MUST_NOT_LEAK" not in excerpt


def test_project_slugging_prose_is_not_treated_as_metadata(tmp_path):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [10:00:00] session-end | prose-session\n"
        "- Project slugging is normal explanatory text.\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, None)

    assert "Project slugging is normal explanatory text." in excerpt


@pytest.mark.parametrize(
    "wrapper",
    ("<analysis>{}</analysis>", "<summary>{}</summary>"),
)
def test_recognized_wrappers_are_removed_before_heading_boundary_detection(
    tmp_path, wrapper
):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | older-session\n"
        "OLDER_LEGACY_CONTENT\n"
        f"{wrapper.format('## [10:00:00] session-end | newer-session')}\n"
        "NEWER_LEGACY_CONTENT\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )
    excerpt = session_start_context.daily_excerpt(daily, None)

    assert len(records) == 2
    assert [record.source_position for record in records] == [0, 2]
    assert "NEWER_LEGACY_CONTENT" in excerpt
    assert "OLDER_LEGACY_CONTENT" not in excerpt


@pytest.mark.parametrize(
    "wrapper",
    (
        "<analysis>{}</analysis>",
        "<summary>{}</summary>",
        "<ANALYSIS><summary>{}</summary></ANALYSIS>",
    ),
)
def test_wrapped_project_slug_remains_explicitly_scoped(tmp_path, wrapper):
    import session_start_context

    beta_root = (tmp_path / "beta").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy-session\n"
        "GENUINE_LEGACY_FALLBACK\n\n"
        "## [10:00:00] session-end | beta-session\n"
        f"{wrapper.format('- Project slug: `beta`')}\n"
        f"{wrapper.format(f'- Project root JSON: {json.dumps(str(beta_root))}')}\n\n"
        "WRAPPED_BETA_CONTENT_MUST_NOT_LEAK\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )

    assert len(records) == 2
    assert records[1].slug == "beta"
    for active_slug in (None, "alpha"):
        excerpt = session_start_context.daily_excerpt(daily, active_slug)
        if active_slug is None:
            assert "GENUINE_LEGACY_FALLBACK" in excerpt
        else:
            assert "GENUINE_LEGACY_FALLBACK" not in excerpt
        assert "WRAPPED_BETA_CONTENT_MUST_NOT_LEAK" not in excerpt


@pytest.mark.parametrize(
    ("wrapper", "kind", "suffix", "slug", "body"),
    (
        ("<analysis>{}</analysis>", "tool", " | Edit", "beta", "WRAPPED_TOOL_TARGET"),
        ("<summary>{}</summary>", "tool", " | Edit", "beta", "WRAPPED_TOOL_TARGET"),
        ("<analysis>{}</analysis>", "prompt", "", "alpha", "WRAPPED_ALPHA_PROMPT"),
        ("<summary>{}</summary>", "prompt", "", "alpha", "WRAPPED_ALPHA_PROMPT"),
    ),
)
def test_wrapped_compact_bullets_are_classified_before_heading_membership(
    tmp_path, wrapper, kind, suffix, slug, body
):
    import session_start_context

    project_root = (tmp_path / slug).resolve()
    compact = (
        f"- `[10:00:00] {kind} | ses_1234 | {slug}{suffix}` "
        f"project-root-json={json.dumps(str(project_root))} | {body}"
    )
    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy-session\n"
        "GENUINE_LEGACY_FALLBACK\n"
        f"{wrapper.format(compact)}\n",
        encoding="utf-8",
    )

    records = session_start_context.parse_daily_records(
        daily.read_text(encoding="utf-8")
    )

    assert [record.kind for record in records] == ["heading", kind]
    assert [record.order for record in records] == [0, 1]
    assert body not in "\n".join(records[0].lines)
    if kind == "tool":
        excerpt = session_start_context.daily_excerpt(daily, "beta", project_root)
        assert "GENUINE_LEGACY_FALLBACK" in excerpt
        assert body not in excerpt
    else:
        excerpt = session_start_context.daily_excerpt(daily, "alpha", project_root)
        assert body in excerpt


def test_unknown_markup_remains_visible_only_inside_untrusted_daily_data(tmp_path):
    import session_start_context

    daily = tmp_path / "2026-07-28.md"
    daily.write_text(
        "## [09:00:00] session-end | legacy-session\n"
        "<instructions>UNKNOWN_MARKUP_DATA</instructions>\n",
        encoding="utf-8",
    )

    excerpt = session_start_context.daily_excerpt(daily, None)

    assert "<instructions>UNKNOWN_MARKUP_DATA</instructions>" in excerpt
    assert "UNTRUSTED" in excerpt


def test_latest_useful_daily_skips_empty_newest_file(monkeypatch, tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-07-27.md").write_text(
        "- `[09:00:00] prompt | ses_1234 | alpha` "
        f"project-root-json={json.dumps(str(project_root))} | OLDER_USEFUL_PROMPT\n",
        encoding="utf-8",
    )
    (daily_dir / "2026-07-28.md").write_text(
        "- `[broken prompt | missing fields` MALFORMED_NEWEST\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index.md")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log.md")
    monkeypatch.setattr(session_start_context, "_resolve_project", lambda _directory: ("alpha", None))
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda _slug=None, _project_root=None: "",
    )
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "## Health\n\nhealthy")
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "",
    )

    context = session_start_context.build_context(tmp_path / "alpha")

    assert "## Latest daily log: 2026-07-27.md" in context
    assert "OLDER_USEFUL_PROMPT" in context
    assert "MALFORMED_NEWEST" not in context


def test_latest_useful_daily_accepts_newer_legacy_fallback_for_active_project(
    monkeypatch, tmp_path
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    older = daily_dir / "2026-07-27.md"
    older.write_text(
        "## [09:00:00] session-end | alpha-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "OLDER_MATCHING_SUMMARY\n",
        encoding="utf-8",
    )
    newer = daily_dir / "2026-07-28.md"
    newer.write_text(
        "## [10:00:00] session-end | legacy-session\n"
        "NEWER_LEGACY_SUMMARY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)

    selected = session_start_context._latest_useful_daily("alpha", project_root)

    assert selected is not None
    selected_path, record = selected
    rendered = session_start_context._render_daily_record(record)
    assert selected_path == newer
    assert "NEWER_LEGACY_SUMMARY" in rendered
    assert "OLDER_MATCHING_SUMMARY" not in rendered


def test_daily_record_reader_uses_a_fixed_size_tail(monkeypatch, tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    prompt = (
        "- `[10:00:00] prompt | ses_1234 | alpha` "
        f"project-root-json={json.dumps(str(project_root))} | TAIL_PROMPT\n"
    ).encode()
    max_tail_bytes = 256 * 1024
    daily = tmp_path / "2026-07-28.md"
    daily.write_bytes(
        b"## [08:00:00] session-end | old\n"
        + b"x" * (max_tail_bytes + 4096)
        + b"\n"
        + prompt
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
        return TrackingFile(handle) if path == daily else handle

    monkeypatch.setattr(Path, "open", tracking_open)

    records = session_start_context._read_daily_records(daily)
    selected = session_start_context._select_daily_record(
        records,
        "alpha",
        project_root,
    )

    assert selected is not None
    assert "TAIL_PROMPT" in session_start_context._render_daily_record(selected)
    assert read_sizes
    assert all(0 <= size <= max_tail_bytes for size in read_sizes)


def test_daily_tail_keeps_complete_record_aligned_with_window_start(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    prompt = (
        "- `[10:00:00] prompt | ses_1234 | alpha` "
        f"project-root-json={json.dumps(str(project_root))} | ALIGNED_PROMPT\n"
    ).encode()
    tail = prompt + b"x" * (session_start_context.DAILY_TAIL_BYTES - len(prompt))
    daily = tmp_path / "2026-07-28.md"
    daily.write_bytes(b"ignored prefix\n" + tail)

    records = session_start_context._read_daily_records(daily)
    selected = session_start_context._select_daily_record(
        records,
        "alpha",
        project_root,
    )

    assert selected is not None
    assert "ALIGNED_PROMPT" in session_start_context._render_daily_record(selected)


def test_invalid_utf8_in_both_scope_keys_cannot_create_alpha_legacy_fallback(
    tmp_path,
):
    import session_start_context

    alpha_root = (tmp_path / "alpha").resolve()
    beta_root = (tmp_path / "beta").resolve()
    daily = tmp_path / "2026-07-28.md"
    daily.write_bytes(
        b"## [10:00:00] session-end | beta-session\n"
        b"- Pro\xffject slug: `beta`\n"
        b"- Pro\xffject root JSON: "
        + json.dumps(str(beta_root)).encode("utf-8")
        + b"\n\nBETA_SECRET_MUST_NOT_FALL_BACK_TO_ALPHA\n"
    )

    records = session_start_context._read_daily_records(daily)
    excerpt = session_start_context.daily_excerpt(daily, "alpha", alpha_root)

    assert records == []
    assert "BETA_SECRET_MUST_NOT_FALL_BACK_TO_ALPHA" not in excerpt
    assert "no eligible" in excerpt
    assert session_start_context._latest_useful_daily(
        "alpha",
        alpha_root,
        [daily],
    ) is None


def test_invalid_utf8_body_fails_closed_for_excerpt_latest_and_hook(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    older = daily_dir / "2026-07-27.md"
    older.write_text(
        "## [09:00:00] session-end | safe-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "OLDER_SAFE_ALPHA_SUMMARY\n",
        encoding="utf-8",
    )
    newer = daily_dir / "2026-07-28.md"
    newer.write_bytes(
        b"## [10:00:00] session-end | malformed-body\n"
        b"- Project slug: `alpha`\n"
        b"- Project root JSON: "
        + json.dumps(str(project_root)).encode("utf-8")
        + b"\n\nINVALID_BODY_\xff_SECRET_MUST_NOT_APPEAR\n"
    )
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log")
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _active: ("alpha", None),
    )
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda _slug=None, _project_root=None: "",
    )
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Health\n\nhealthy",
    )
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "",
    )

    assert session_start_context._read_daily_records(newer) == []
    excerpt = session_start_context.daily_excerpt(newer, "alpha", project_root)
    selected = session_start_context._latest_useful_daily(
        "alpha",
        project_root,
        [newer, older],
    )
    context = session_start_context.build_context(project_root)

    assert "SECRET_MUST_NOT_APPEAR" not in excerpt
    assert "no eligible" in excerpt
    assert selected is not None
    assert selected[0] == older
    assert "OLDER_SAFE_ALPHA_SUMMARY" in context
    assert "SECRET_MUST_NOT_APPEAR" not in context


def test_valid_multibyte_record_survives_tail_start_inside_utf8_sequence(tmp_path):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    record = (
        "## [10:00:00] session-end | alpha-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        "VALID_MULTIBYTE_SUMMARY_Привет\n"
    ).encode()
    filler_size = session_start_context.DAILY_TAIL_BYTES - len(record) - 3
    assert filler_size > 0
    raw_tail = b"\x82\xac" + (b"x" * filler_size) + b"\n" + record
    assert len(raw_tail) == session_start_context.DAILY_TAIL_BYTES
    payload = b"\xe2" + raw_tail
    assert payload.decode("utf-8").startswith("€")
    daily = tmp_path / "2026-07-28.md"
    daily.write_bytes(payload)

    records = session_start_context._read_daily_records(daily)
    selected = session_start_context._select_daily_record(
        records,
        "alpha",
        project_root,
    )

    assert selected is not None
    assert "VALID_MULTIBYTE_SUMMARY_Привет" in session_start_context._render_daily_record(
        selected
    )


def test_file_hash_streams_fixed_size_chunks_with_sha256_parity(monkeypatch, tmp_path):
    import memory_state

    payload = (b"streamed-hash-content\n" * 70_000) + b"final"
    source = tmp_path / "large-daily.md"
    source.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    real_read_bytes = Path.read_bytes
    real_open = Path.open
    read_sizes: list[int] = []

    def reject_whole_file_read(path):
        if path == source:
            raise AssertionError("file_hash must not allocate the whole file")
        return real_read_bytes(path)

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
        return TrackingFile(handle) if path == source else handle

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file_read)
    monkeypatch.setattr(Path, "open", tracking_open)

    assert memory_state.file_hash(source) == expected
    assert len(read_sizes) > 2
    assert read_sizes[0] > 0
    assert set(read_sizes) == {read_sizes[0]}
    assert read_sizes[0] < len(payload)


def test_daily_excerpt_keeps_its_more_lines_marker_after_final_budgeting(
    monkeypatch, tmp_path
):
    import session_start_context

    project_root = (tmp_path / "alpha").resolve()
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    body = "\n".join(f"SUMMARY_LINE_{index} " + "x" * 70 for index in range(12))
    (daily_dir / "2026-07-28.md").write_text(
        "## [09:00:00] session-end | session-alpha\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(str(project_root))}\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index.md")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log.md")
    monkeypatch.setattr(session_start_context, "_resolve_project", lambda _directory: ("alpha", None))
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda _slug=None, _project_root=None: "",
    )
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "## Health\n\nhealthy")
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "",
    )

    context = session_start_context.build_context(tmp_path / "alpha")
    daily_section = context.split("## Latest daily log", 1)[1].split(
        "## Recent knowledge/log.md", 1
    )[0]

    assert "more lines)" in daily_section
    assert "section truncated" not in daily_section


def test_session_start_reads_index_log_and_project_state_with_explicit_bounds(
    monkeypatch, tmp_path
):
    import session_start_context

    index = tmp_path / "index.md"
    log = tmp_path / "log.md"
    project_root = tmp_path / "alpha"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "alpha" / "state.md"
    state_path.parent.mkdir(parents=True)
    index.write_text("# Index\n\n## Entry points\n- [[one]]\n", encoding="utf-8")
    log.write_text("- 2026-07-28 - bounded log\n", encoding="utf-8")
    state_path.write_text(
        "# Alpha\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        '- Runtime slug JSON: "alpha"\n'
        "## Where we left off\nBounded state\n",
        encoding="utf-8",
    )
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", index)
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", log)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _directory: ("alpha", state_path),
    )
    monkeypatch.setattr(session_start_context, "guardrails_block", lambda *_args: "")
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "health")
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "",
    )
    real_read_text = Path.read_text
    real_open = Path.open
    read_sizes: list[int] = []

    def reject_unbounded_read(path, *args, **kwargs):
        if path in {index, log, state_path}:
            raise AssertionError(f"unbounded SessionStart read: {path.name}")
        return real_read_text(path, *args, **kwargs)

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
        return TrackingFile(handle) if path in {index, log, state_path} else handle

    monkeypatch.setattr(Path, "read_text", reject_unbounded_read)
    monkeypatch.setattr(Path, "open", tracking_open)

    context = session_start_context.build_context(project_root)

    assert "Bounded state" in context
    assert read_sizes
    assert all(size >= 0 for size in read_sizes)


def test_standalone_project_state_context_reads_state_with_explicit_bound(
    monkeypatch, tmp_path
):
    import session_start_project_state

    project_root = tmp_path / "alpha"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "alpha" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Alpha\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        '- Runtime slug JSON: "alpha"\n'
        "## Where we left off\nBOUNDED_STANDALONE_STATE\n",
        encoding="utf-8",
    )
    real_read_text = Path.read_text
    real_fdopen = os.fdopen
    read_sizes: list[int] = []

    def reject_read_text(path, *args, **kwargs):
        if path == state_path:
            raise AssertionError("standalone state read must be bounded")
        return real_read_text(path, *args, **kwargs)

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

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    monkeypatch.setattr(os, "fdopen", tracking_fdopen)

    context = session_start_project_state._build_context(
        state_path,
        "alpha",
        False,
        project_root,
    )

    assert "BOUNDED_STANDALONE_STATE" in context
    assert read_sizes and all(size >= 0 for size in read_sizes)


@pytest.mark.parametrize(
    ("runtime_state", "expect_corrupt_backup"),
    (
        ("[]", True),
        ('"scalar"', True),
        ("null", True),
        (
            '{"last_compile_audit":["bad"],'
            '"flush_tier_counts":{"major":[],"minor":{},"ok":"bad"}}',
            False,
        ),
    ),
)
def test_session_start_survives_malformed_runtime_state(
    tmp_path, runtime_state, expect_corrupt_backup
):
    vault = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    state_file = state_root / "run" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(runtime_state, encoding="utf-8")
    output = tmp_path / "context.md"
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
    }

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output-file", str(output)],
        cwd=SCRIPT.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert "# Project memory context" in output.read_text(encoding="utf-8")
    quarantines = list(state_file.parent.glob("state.json.corrupt-*"))
    assert bool(quarantines) is expect_corrupt_backup
    if expect_corrupt_backup:
        assert not state_file.exists()
        assert quarantines[0].read_text(encoding="utf-8") == runtime_state


def test_oversized_invalid_state_is_quarantined_once_without_full_copy(
    monkeypatch,
    tmp_path,
):
    import shutil

    import memory_state

    state_dir = tmp_path / "run"
    state_file = state_dir / "state.json"
    reports = tmp_path / "logs"
    state_dir.mkdir()
    payload = b'{"private":"' + (b"x" * 256)
    state_file.write_bytes(payload)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "REPORTS_DIR", reports)
    monkeypatch.setattr(memory_state, "MAX_STATE_JSON_CHARS", 64)
    copy_calls: list[tuple[object, object]] = []
    real_copyfile = shutil.copyfile

    def tracking_copyfile(source, destination, *args, **kwargs):
        copy_calls.append((source, destination))
        return real_copyfile(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", tracking_copyfile)

    first = memory_state.load_state()
    quarantines_after_first = list(state_dir.glob("state.json.corrupt-*"))
    second = memory_state.load_state()
    quarantines_after_second = list(state_dir.glob("state.json.corrupt-*"))

    assert first == second == {}
    assert copy_calls == []
    assert not state_file.exists()
    assert quarantines_after_second == quarantines_after_first
    assert len(quarantines_after_first) == 1
    assert quarantines_after_first[0].read_bytes() == payload


def test_update_state_refuses_bytes_beyond_reader_limit_without_replacing_state(
    monkeypatch,
    tmp_path,
):
    import memory_state

    state_dir = tmp_path / "run"
    state_file = state_dir / "state.json"
    state_dir.mkdir()
    original = b'{"durable":"preserve"}\n'
    state_file.write_bytes(original)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    monkeypatch.setattr(memory_state, "MAX_STATE_JSON_CHARS", 96)

    with pytest.raises(ValueError, match="state JSON exceeds"):
        memory_state.update_state(
            lambda state: state.update({"oversized": "\u00e9" * 64})
        )

    assert state_file.read_bytes() == original


@pytest.mark.parametrize(
    "read_error",
    (PermissionError("state read denied"), OSError("state read unavailable")),
)
def test_update_state_preserves_existing_bytes_when_state_read_fails(
    monkeypatch,
    tmp_path,
    read_error,
):
    import memory_state

    state_dir = tmp_path / "run"
    state_file = state_dir / "state.json"
    state_dir.mkdir()
    original = b'{\n  "durable": "preserve exactly",\n  "counter": 7\n}\n'
    state_file.write_bytes(original)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    real_open = Path.open
    mutator_calls = []

    def denied_state_read(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == state_file and "r" in mode:
            raise read_error
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_state_read)

    with pytest.raises(type(read_error), match=str(read_error)):
        memory_state.update_state(
            lambda state: mutator_calls.append(dict(state)) or state.clear()
        )

    assert mutator_calls == []
    with open(state_file, "rb") as handle:
        assert handle.read() == original


def test_load_state_documents_safe_degradation_without_mutating_existing_bytes(
    monkeypatch,
    tmp_path,
):
    import memory_state

    state_dir = tmp_path / "run"
    state_file = state_dir / "state.json"
    state_dir.mkdir()
    original = b'{"durable":"read-only fallback"}\n'
    state_file.write_bytes(original)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    real_open = Path.open

    def denied_state_read(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == state_file and "r" in mode:
            raise PermissionError("read-only state fallback")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_state_read)

    assert "read-only" in (memory_state.load_state.__doc__ or "").lower()
    assert memory_state.load_state() == {}
    with open(state_file, "rb") as handle:
        assert handle.read() == original
    assert list(state_dir.glob("state.json.corrupt-*")) == []


def test_invalid_utf8_state_is_quarantined_once(monkeypatch, tmp_path):
    import memory_state

    state_dir = tmp_path / "run"
    state_file = state_dir / "state.json"
    reports = tmp_path / "logs"
    state_dir.mkdir()
    payload = b'{"durable":"\xff"}'
    state_file.write_bytes(payload)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", reports)

    assert memory_state.load_state() == {}
    first_quarantines = list(state_dir.glob("state.json.corrupt-*"))
    assert memory_state.load_state() == {}

    assert not state_file.exists()
    assert len(first_quarantines) == 1
    assert first_quarantines[0].read_bytes() == payload
    assert list(state_dir.glob("state.json.corrupt-*")) == first_quarantines


def test_invalid_state_reader_cannot_quarantine_concurrent_valid_update(
    monkeypatch,
    tmp_path,
):
    import memory_state

    state_dir = tmp_path / "run"
    state_file = state_dir / "state.json"
    state_dir.mkdir()
    state_file.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")

    gate = threading.Lock()
    ownership = threading.local()
    invalid_read = threading.Event()
    writer_attempting = threading.Event()
    writer_finished = threading.Event()
    failures: list[BaseException] = []

    @contextmanager
    def controlled_state_lock(*_args, **_kwargs):
        is_writer = threading.current_thread().name == "state-writer"
        if is_writer:
            writer_attempting.set()
        if not gate.acquire(timeout=5):
            raise TimeoutError("test state lock timed out")
        ownership.held = True
        try:
            yield
        finally:
            ownership.held = False
            gate.release()

    real_loads = memory_state.json.loads

    def coordinated_loads(raw, *args, **kwargs):
        if threading.current_thread().name == "state-reader":
            invalid_read.set()
            if not writer_attempting.wait(timeout=5):
                raise TimeoutError("writer did not attempt the state lock")
            if not getattr(ownership, "held", False) and not writer_finished.wait(
                timeout=5
            ):
                raise TimeoutError("unlocked writer did not publish state")
        return real_loads(raw, *args, **kwargs)

    monkeypatch.setattr(memory_state, "_state_lock", controlled_state_lock)
    monkeypatch.setattr(memory_state.json, "loads", coordinated_loads)

    def read_invalid_state():
        try:
            memory_state.load_state()
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    def publish_valid_state():
        try:
            memory_state.update_state(lambda state: state.update({"writer": "valid"}))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
        finally:
            writer_finished.set()

    reader = threading.Thread(target=read_invalid_state, name="state-reader")
    writer = threading.Thread(target=publish_valid_state, name="state-writer")
    reader.start()
    assert invalid_read.wait(timeout=5)
    writer.start()
    reader.join(timeout=5)
    writer.join(timeout=5)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert failures == []
    assert memory_state.load_state() == {"writer": "valid"}


def test_session_start_main_permission_error_emits_empty_hook_json(
    monkeypatch,
    tmp_path,
    capsys,
):
    import session_start_context

    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        session_start_context,
        "_active_project_directory",
        lambda _explicit: session_start_context.ProjectRootResolution(None, True),
    )

    def denied(*_args, **_kwargs):
        raise PermissionError("private filesystem detail")

    monkeypatch.setattr(session_start_context, "build_context", denied)

    assert session_start_context.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "",
        }
    }


def test_output_file_build_failure_replaces_stale_private_context(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    output = tmp_path / "cache" / "session-context.md"
    output.parent.mkdir()
    output.write_text("PRIVATE_PROJECT_CONTEXT\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--output-file", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", tmp_path / "logs")

    def denied(*_args, **_kwargs):
        raise PermissionError("private filesystem detail")

    monkeypatch.setattr(session_start_context, "build_context", denied)

    assert session_start_context.main() == 0
    assert output.read_text(encoding="utf-8") == ""


def test_output_file_build_failure_removes_stale_target_if_empty_replace_fails(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    output = tmp_path / "cache" / "session-context.md"
    output.parent.mkdir()
    output.write_text("PRIVATE_PROJECT_CONTEXT\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--output-file", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", tmp_path / "logs")

    def denied(*_args, **_kwargs):
        raise PermissionError("private filesystem detail")

    def failed_replace(*_args, **_kwargs):
        raise OSError("replace unavailable")

    monkeypatch.setattr(session_start_context, "build_context", denied)
    monkeypatch.setattr(session_start_context, "atomic_write", failed_replace)

    assert session_start_context.main() == 0
    assert not output.exists()


def test_output_file_build_failure_preserves_directory_target(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    output = tmp_path / "cache" / "session-context.md"
    output.mkdir(parents=True)
    marker = output / "keep.txt"
    marker.write_text("KEEP_DIRECTORY\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--output-file", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        session_start_context,
        "build_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    assert session_start_context.main() != 0
    assert output.is_dir()
    assert marker.read_text(encoding="utf-8") == "KEEP_DIRECTORY\n"


def test_output_file_build_failure_preserves_symlink_target(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    outside = tmp_path / "outside.md"
    outside.write_text("KEEP_SYMLINK_TARGET\n", encoding="utf-8")
    output = tmp_path / "cache" / "session-context.md"
    output.parent.mkdir()
    emulated_symlink = False
    try:
        output.symlink_to(outside)
    except OSError:
        emulated_symlink = True
        real_lstat = Path.lstat

        class SymlinkMetadata:
            st_mode = 0o120000

        def symlink_lstat(path, *args, **kwargs):
            if path == output:
                return SymlinkMetadata()
            return real_lstat(path, *args, **kwargs)

        def forbidden_mutation(*_args, **_kwargs):
            raise AssertionError("symlink output must not be mutated")

        monkeypatch.setattr(Path, "lstat", symlink_lstat)
        monkeypatch.setattr(session_start_context, "atomic_write", forbidden_mutation)
        monkeypatch.setattr(Path, "unlink", forbidden_mutation)
        monkeypatch.setattr(session_start_context.os, "replace", forbidden_mutation)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--output-file", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        session_start_context,
        "build_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    assert session_start_context.main() != 0
    if not emulated_symlink:
        assert output.is_symlink()
    assert outside.read_text(encoding="utf-8") == "KEEP_SYMLINK_TARGET\n"


def test_output_file_build_failure_is_nonzero_when_regular_target_cannot_be_cleared(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    output = tmp_path / "cache" / "session-context.md"
    output.parent.mkdir()
    output.write_text("PRIVATE_PROJECT_CONTEXT\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--output-file", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(session_start_context, "DEBUG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        session_start_context,
        "build_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(
        session_start_context,
        "atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write denied")),
    )
    real_unlink = Path.unlink

    def denied_unlink(path, *args, **kwargs):
        if path == output:
            raise PermissionError("unlink denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    monkeypatch.setattr(
        session_start_context.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rename denied")),
    )

    assert session_start_context.main() != 0
    assert output.read_text(encoding="utf-8") == "PRIVATE_PROJECT_CONTEXT\n"


def test_bounded_block_never_splits_markdown_lines_and_handles_tiny_budgets():
    import session_start_context

    prefix = "## Heading\n\nfirst complete line"
    marker = "\n... (section truncated)"
    block = prefix + "\nSECOND_LINE_MUST_NOT_BE_PARTIAL\nthird line"

    bounded = session_start_context._bounded_block(block, len(prefix) + len(marker))

    assert bounded == prefix + marker
    assert "SECOND_LINE" not in bounded
    assert session_start_context._bounded_block(block, 1) == "## Heading"
    assert session_start_context._bounded_block(block, 0) == "## Heading"
    assert session_start_context._bounded_block("", -1) == ""


def test_combined_project_block_exposes_bounded_untrusted_bootstrap(
    monkeypatch,
    tmp_path,
):
    import session_start_context

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
    bootstrap_path = state_path.with_name("bootstrap.md")
    fingerprint = bootstrap_project._bootstrap_source_fingerprint(project_root, None)
    assert fingerprint is not None
    bootstrap_path.write_text(
        "---\ntype: bootstrap-context\n"
        'project_slug_json: "alpha"\n'
        f"project_root_json: {json.dumps(str(project_root.resolve()))}\n"
        f"project_state_path_json: {json.dumps(str(state_path.resolve()))}\n"
        "git_head_json: null\n"
        "bootstrap_schema_json: 2\n"
        f"source_fingerprint_json: {json.dumps(fingerprint)}\n"
        "---\n\n"
        "# Alpha bootstrap\n\nBOOTSTRAP_CONTEXT_SENTINEL\n"
        + ("x" * 3_000),
        encoding="utf-8",
    )
    real_open = Path.open
    bootstrap_read_sizes: list[int] = []

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
            bootstrap_read_sizes.append(size)
            return self.handle.read(size)

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == bootstrap_path and "r" in mode:
            return TrackingFile(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    block = session_start_context._project_state_block(
        "alpha",
        state_path,
        project_root,
    )
    bounded = session_start_context._bounded_block(
        block,
        session_start_context.SECTION_BUDGETS["project"],
    )

    assert len(bounded) <= session_start_context.SECTION_BUDGETS["project"]
    assert "## Current project state" in bounded
    assert f"- Project root JSON: {json.dumps(str(project_root.resolve()))}" in bounded
    assert "Project bootstrap" in bounded
    assert "UNTRUSTED" in bounded
    assert "BOOTSTRAP_CONTEXT_SENTINEL" in bounded
    assert bootstrap_read_sizes
    assert all(0 <= size <= 8193 for size in bootstrap_read_sizes)


def test_combined_context_reserves_complete_288_char_posix_identity(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    posix_root = "/" + ("p" * 287)
    assert len(posix_root) == 288
    root_line = f"- Project root JSON: {json.dumps(posix_root)}"
    slug_line = '- Runtime slug JSON: "long-posix"'

    context = _render_combined_identity_context(
        monkeypatch,
        tmp_path,
        f"{root_line}\n{slug_line}",
        slug="long-posix",
    )

    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS
    assert root_line in context.splitlines()
    assert slug_line in context.splitlines()
    assert context.index(root_line) < context.index("COMBINED_PRIORITY_HANDOFF_")
    assert "COMBINED_SECONDARY_DETAIL_" not in context


def test_combined_context_omits_project_when_identity_exceeds_total_budget(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    root_prefix = "OVER_TOTAL_COMBINED_ROOT_"
    root_line = f'- Project root JSON: "/{root_prefix}{"x" * 3_000}"'
    slug_line = '- Runtime slug JSON: "over-total-combined"'

    context = _render_combined_identity_context(
        monkeypatch,
        tmp_path,
        f"{root_line}\n{slug_line}",
        slug="over-total-combined",
    )

    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS
    assert session_start_context.CONTEXT_HEADING in context
    assert "## Current project state" not in context
    assert root_prefix not in context
    assert slug_line not in context


def test_combined_context_emits_complete_marker_when_only_marker_fits(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    slug = "marker-floor"
    identity, root_line, slug_line = _combined_identity_at_marker_boundary(
        session_start_context,
        slug,
    )

    context = _render_combined_identity_context(
        monkeypatch,
        tmp_path,
        identity,
        slug=slug,
    )

    assert len(context) == session_start_context.MAX_CONTEXT_CHARS
    assert root_line in context.splitlines()
    assert slug_line in context.splitlines()
    assert context.splitlines()[-1] == session_start_context.SECTION_TRUNCATION_MARKER
    assert context.count(session_start_context.SECTION_TRUNCATION_MARKER) == 1
    assert "COMBINED_PRIORITY_HANDOFF_" not in context


def test_combined_context_omits_project_when_required_marker_cannot_fit(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    slug = "marker-overflow"
    identity, root_line, slug_line = _combined_identity_at_marker_boundary(
        session_start_context,
        slug,
        extra_chars=1,
    )
    mandatory = "\n\n".join(
        ("## Current project state", f"**Project:** `{slug}`", identity)
    )
    identity_only = f"{session_start_context.CONTEXT_HEADING}\n\n{mandatory}\n"
    required = (
        f"{session_start_context.CONTEXT_HEADING}\n\n{mandatory}\n\n"
        f"{session_start_context.SECTION_TRUNCATION_MARKER}\n"
    )
    assert len(identity_only) <= session_start_context.MAX_CONTEXT_CHARS
    assert len(required) == session_start_context.MAX_CONTEXT_CHARS + 1

    context = _render_combined_identity_context(
        monkeypatch,
        tmp_path,
        identity,
        slug=slug,
    )

    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS
    assert "## Current project state" not in context
    assert root_line not in context
    assert slug_line not in context


def test_combined_context_emits_complete_short_secondary_at_exact_boundary(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    slug = "exact-short-secondary"
    secondary = "EXACT"
    assert len(secondary) < len(session_start_context.SECTION_TRUNCATION_MARKER)
    root_prefix = '- Project root JSON: "/'
    identity_suffix = f'"\n- Runtime slug JSON: {json.dumps(slug)}'
    base_identity = f"{root_prefix}{identity_suffix}"
    base_mandatory = "\n\n".join(
        ("## Current project state", f"**Project:** `{slug}`", base_identity)
    )
    base_context = (
        f"{session_start_context.CONTEXT_HEADING}\n\n{base_mandatory}\n\n"
        f"{secondary}\n"
    )
    filler_size = session_start_context.MAX_CONTEXT_CHARS - len(base_context)
    assert filler_size > 0
    identity = f'{root_prefix}{"s" * filler_size}{identity_suffix}'
    mandatory = "\n\n".join(
        ("## Current project state", f"**Project:** `{slug}`", identity)
    )
    expected = (
        f"{session_start_context.CONTEXT_HEADING}\n\n{mandatory}\n\n"
        f"{secondary}\n"
    )
    marker_context = (
        f"{session_start_context.CONTEXT_HEADING}\n\n{mandatory}\n\n"
        f"{session_start_context.SECTION_TRUNCATION_MARKER}\n"
    )
    assert len(expected) == session_start_context.MAX_CONTEXT_CHARS
    assert len(marker_context) > session_start_context.MAX_CONTEXT_CHARS

    context = _render_combined_identity_context(
        monkeypatch,
        tmp_path,
        identity,
        slug=slug,
        project_secondary=secondary,
    )

    assert context == expected
    assert session_start_context.SECTION_TRUNCATION_MARKER not in context


def test_combined_context_redistributes_to_zero_budget_candidate_sections(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    slug = "redistribute-zero"
    root_prefix = '- Project root JSON: "/'
    identity_suffix = f'"\n- Runtime slug JSON: {json.dumps(slug)}'
    filler_size = 1_000 - len(root_prefix) - len(identity_suffix)
    assert filler_size > 0
    identity = f'{root_prefix}{"r" * filler_size}{identity_suffix}'
    assert len(identity) == 1_000
    assert session_start_context.MAX_CONTEXT_CHARS == 2_200

    context = _render_combined_identity_context(
        monkeypatch,
        tmp_path,
        identity,
        slug=slug,
        payload_size=0,
    )

    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS
    assert "## Latest daily log: synthetic" in context
    assert "LOG_SECONDARY_" in context
    assert "INDEX_SECONDARY_" in context
    assert context.index("## Latest daily log: synthetic") < context.index(
        "LOG_SECONDARY_"
    ) < context.index("INDEX_SECONDARY_")


def test_combined_project_block_prioritizes_saved_handoff_over_bootstrap(
    tmp_path,
):
    import session_start_context

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
        + ("STATE_DETAIL_TOO_LARGE " + "x" * 2_000 + "\n"),
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
        + ("y" * 2_000),
        encoding="utf-8",
    )

    block = session_start_context._project_state_block(
        "alpha",
        state_path,
        project_root,
    )
    bounded = session_start_context._bounded_block(
        block,
        session_start_context.SECTION_BUDGETS["project"],
    )

    assert "SAVED_HANDOFF_SENTINEL" in bounded
    assert "BOOTSTRAP_MUST_YIELD_SENTINEL" not in bounded


def test_combined_project_block_truncates_overlong_handoff_line_before_detail(
    tmp_path: Path,
):
    import session_start_context

    project_root = tmp_path / "project-root"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "long-handoff" / "state.md"
    state_path.parent.mkdir(parents=True)
    handoff_prefix = "HANDOFF_500_CHAR_PREFIX_"
    handoff_line = handoff_prefix + ("h" * (500 - len(handoff_prefix)))
    assert len(handoff_line) == 500
    state_path.write_text(
        "# Long handoff state\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        '- Runtime slug JSON: "long-handoff"\n'
        "## Where we left off\n"
        f"{handoff_line}\n"
        "## Recent decisions\n"
        "LOWER_PRIORITY_DETAIL_MUST_NOT_DISPLACE_HANDOFF\n",
        encoding="utf-8",
    )

    block = session_start_context._project_state_block(
        "long-handoff",
        state_path,
        project_root,
    )
    bounded = session_start_context._bounded_block(
        block,
        session_start_context.SECTION_BUDGETS["project"],
    )

    assert len(bounded) <= session_start_context.SECTION_BUDGETS["project"]
    assert handoff_prefix in bounded
    assert session_start_context.SECTION_TRUNCATION_MARKER in bounded
    assert "LOWER_PRIORITY_DETAIL_MUST_NOT_DISPLACE_HANDOFF" not in bounded


def test_combined_project_block_preserves_identity_before_clipped_unicode(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    project_root = tmp_path / "project-root"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "unicode-combined" / "state.md"
    state_path.parent.mkdir(parents=True)
    handoff = "COMBINED_HANDOFF_START_" + ("界🙂" * 400) + "_COMBINED_HANDOFF_END"
    bootstrap = "COMBINED_BOOTSTRAP_START_" + ("λ🚀" * 800)
    state_path.write_text(
        "# Unicode combined state\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        '- Runtime slug JSON: "unicode-combined"\n'
        "## Where we left off\n"
        f"{handoff}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        session_start_context,
        "_read_bootstrap_context",
        lambda *_args, **_kwargs: bootstrap,
    )

    block = session_start_context._project_state_block(
        "unicode-combined",
        state_path,
        project_root,
    )
    bounded = session_start_context._bounded_block(
        block,
        session_start_context.SECTION_BUDGETS["project"],
    )

    root_line = f"- Project root JSON: {json.dumps(str(project_root.resolve()))}"
    slug_line = '- Runtime slug JSON: "unicode-combined"'
    assert len(bounded) <= session_start_context.SECTION_BUDGETS["project"]
    assert root_line in bounded
    assert slug_line in bounded
    assert bounded.index(root_line) < bounded.index("COMBINED_HANDOFF_START_")
    assert bounded.index(slug_line) < bounded.index("COMBINED_HANDOFF_START_")
    assert "_COMBINED_HANDOFF_END" not in bounded
    assert "... (line truncated)" in bounded
    assert session_start_context.SECTION_TRUNCATION_MARKER in bounded
    assert bounded.encode("utf-8").decode("utf-8") == bounded


def test_full_context_preserves_identity_before_clipped_unicode_project_data(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    project_root = tmp_path / "unicode-full"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "unicode-full" / "state.md"
    state_path.parent.mkdir(parents=True)
    handoff = "FULL_HANDOFF_START_" + ("界🙂" * 500) + "_FULL_HANDOFF_END"
    bootstrap = "FULL_BOOTSTRAP_START_" + ("λ🚀" * 1_000) + "_FULL_BOOTSTRAP_END"
    state_path.write_text(
        "# Unicode full state\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        '- Runtime slug JSON: "unicode-full"\n'
        "## Where we left off\n"
        f"{handoff}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _active: ("unicode-full", state_path),
    )
    monkeypatch.setattr(
        session_start_context,
        "_read_bootstrap_context",
        lambda *_args, **_kwargs: bootstrap,
    )
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", tmp_path / "missing-daily")
    monkeypatch.setattr(session_start_context, "_recent_daily_paths", lambda: [])
    monkeypatch.setattr(session_start_context, "guardrails_block", lambda *_args: "")
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\nHEALTH",
    )
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "",
    )

    context = session_start_context.build_context(project_root)

    root_line = f"- Project root JSON: {json.dumps(str(project_root.resolve()))}"
    slug_line = '- Runtime slug JSON: "unicode-full"'
    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS
    assert root_line in context
    assert slug_line in context
    assert context.index(root_line) < context.index("FULL_HANDOFF_START_")
    assert context.index(slug_line) < context.index("FULL_HANDOFF_START_")
    assert "_FULL_BOOTSTRAP_END" not in context
    assert "... (line truncated)" in context
    assert session_start_context.SECTION_TRUNCATION_MARKER in context
    assert context.encode("utf-8").decode("utf-8") == context


@pytest.mark.parametrize("indent", ("", "  "))
def test_state_splitters_keep_same_custom_heading_before_and_after_handoff(
    indent: str,
):
    import session_start_project_state

    body = (
        f"{indent}# Alpha state\n"
        '- Project root JSON: "D:/alpha"\n'
        f"{indent}## Project metadata\n"
        "PRE_CONTENT_METADATA_SENTINEL\n"
        f"{indent}## Where we left off\n"
        "SAVED_HANDOFF_SENTINEL\n"
        f"{indent}## Project metadata\n"
        "POST_CONTENT_CUSTOM_SENTINEL\n"
    )

    identity, remainder = session_start_project_state._split_state_identity(body)
    handoff, detail = session_start_project_state._split_state_handoff(remainder)

    assert "# Alpha state" in identity
    assert "PRE_CONTENT_METADATA_SENTINEL" in identity
    assert "SAVED_HANDOFF_SENTINEL" in handoff
    assert "PRE_CONTENT_METADATA_SENTINEL" not in handoff
    assert "POST_CONTENT_CUSTOM_SENTINEL" not in handoff
    assert "PRE_CONTENT_METADATA_SENTINEL" not in detail
    assert "POST_CONTENT_CUSTOM_SENTINEL" in detail


def test_state_splitter_does_not_promote_handoff_heading_after_content_begins():
    import session_start_project_state

    body = (
        "# Alpha state\n"
        '- Project root JSON: "D:/alpha"\n'
        "## Recent decisions\n"
        "FIRST_CONTENT_SENTINEL\n"
        "## Where we left off\n"
        "LATE_CUSTOM_HANDOFF_SENTINEL\n"
    )

    _identity, remainder = session_start_project_state._split_state_identity(body)
    handoff, detail = session_start_project_state._split_state_handoff(remainder)

    assert handoff == ""
    assert "FIRST_CONTENT_SENTINEL" in detail
    assert "LATE_CUSTOM_HANDOFF_SENTINEL" in detail


def test_advisory_receives_exact_owned_state_path(monkeypatch, tmp_path):
    import build_advisory
    import session_start_context

    project_root = tmp_path / "active-root"
    project_root.mkdir()
    state_path = tmp_path / "projects" / "legacy folder" / "state.md"
    state_path.parent.mkdir(parents=True)
    calls: list[tuple[str | None, Path | None, Path | None]] = []
    monkeypatch.setattr(
        build_advisory,
        "build_advisory",
        lambda slug, *, state_path, project_root: calls.append(
            (slug, state_path, project_root)
        )
        or "ADVICE",
    )

    block = session_start_context.advisory_block(
        "active-safe",
        state_path,
        project_root,
    )

    assert "ADVICE" in block
    assert calls == [("active-safe", state_path, project_root)]


def test_session_context_passes_exact_root_to_guardrails_and_advisory(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    project_root = tmp_path / "workspaces" / "shared"
    project_root.mkdir(parents=True)
    state_path = tmp_path / "projects" / "shared" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("state\n", encoding="utf-8")
    calls: list[tuple] = []
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _active: ("shared", state_path),
    )
    monkeypatch.setattr(session_start_context, "_recent_daily_paths", lambda: [])
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda slug, root: calls.append(("guardrails", slug, root))
        or "## Guard rails\n\nEXACT_GUARDRAILS",
    )
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda slug, state, root, **_kwargs: calls.append(
            ("advisory", slug, state, root)
        )
        or "## Advisory\n\nEXACT_ADVISORY",
    )
    monkeypatch.setattr(
        session_start_context,
        "_project_state_block",
        lambda *_args, **_kwargs: "## Current project state\n\nPROJECT_STATE",
    )
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\nHEALTH",
    )
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log")

    context = session_start_context.build_context(project_root)

    assert "EXACT_GUARDRAILS" in context
    assert "EXACT_ADVISORY" in context
    assert calls == [
        ("guardrails", "shared", project_root.resolve()),
        ("advisory", "shared", state_path, project_root.resolve()),
    ]


def test_open_threads_use_exact_contained_state_not_runtime_slug_folder(
    monkeypatch, tmp_path
):
    import build_advisory

    projects = tmp_path / "projects"
    exact = projects / "legacy folder" / "state.md"
    exact.parent.mkdir(parents=True)
    exact.write_text(
        '- Project root JSON: "D:/active"\n'
        '- Runtime slug JSON: "active-safe"\n'
        "## Open threads\n- EXACT_OWNED_THREAD\n",
        encoding="utf-8",
    )
    decoy = projects / "active-safe" / "state.md"
    decoy.parent.mkdir(parents=True)
    decoy.write_text(
        "## Open threads\n- DECOY_SLUG_FOLDER_THREAD\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside" / "state.md"
    outside.parent.mkdir()
    outside.write_text("## Open threads\n- OUTSIDE_THREAD\n", encoding="utf-8")
    monkeypatch.setattr(build_advisory, "PROJECTS_DIR", projects)

    threads = build_advisory._read_open_threads("active-safe", exact, "D:/active")

    assert threads == ["EXACT_OWNED_THREAD"]
    assert build_advisory._read_open_threads(
        "active-safe",
        outside,
        "D:/active",
    ) == []


def test_open_threads_use_shared_identity_bound_reader_without_reopening_state(
    monkeypatch,
    tmp_path: Path,
):
    import build_advisory

    project_root = tmp_path / "work" / "shared-reader"
    project_root.mkdir(parents=True)
    state_path = tmp_path / "projects" / "shared-reader" / "state.md"
    calls: list[tuple[Path, str, Path]] = []
    body = (
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        '- Runtime slug JSON: "shared-reader"\n'
        "## Open threads\n- SHARED_READER_THREAD\n"
    )

    def shared_reader(path: Path, slug: str, root: Path) -> str:
        calls.append((path, slug, root))
        return body

    monkeypatch.setattr(
        build_advisory,
        "_read_trusted_state_body",
        shared_reader,
        raising=False,
    )
    real_resolve = Path.resolve

    def forbid_state_resolution(path: Path, *args, **kwargs):
        if path == state_path:
            raise AssertionError("advisory must not resolve or reopen state")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", forbid_state_resolution)

    threads = build_advisory._read_open_threads(
        "shared-reader",
        state_path,
        project_root,
    )

    assert threads == ["SHARED_READER_THREAD"]
    assert calls == [(state_path, "shared-reader", project_root)]


def test_open_threads_parser_ignores_fenced_commented_and_raw_html_structure(
    monkeypatch,
    tmp_path: Path,
):
    import build_advisory

    projects = tmp_path / "projects"
    project_root = tmp_path / "work" / "structural-threads"
    project_root.mkdir(parents=True)
    state_path = projects / "structural-threads" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Structural threads state\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        '- Runtime slug JSON: "structural-threads"\n'
        "```markdown\n"
        "## Open threads\n"
        "- FENCED_THREAD_MUST_NOT_APPEAR\n"
        "```\n"
        "<!--\n"
        "## Open threads\n"
        "- COMMENTED_THREAD_MUST_NOT_APPEAR\n"
        "-->\n"
        "<script>\n"
        "## Open threads\n"
        "- RAW_THREAD_MUST_NOT_APPEAR\n"
        "</script>\n"
        "## Open threads\n"
        "- FIRST_VISIBLE_THREAD\n"
        "<div>\n"
        "## Recent decisions\n"
        "- RAW_TERMINATOR_MUST_NOT_APPEAR\n"
        "\n"
        "- SECOND_VISIBLE_THREAD\n"
        "## Recent decisions\n"
        "- visible decision\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "PROJECTS_DIR", projects)

    threads = build_advisory._read_open_threads(
        "structural-threads",
        state_path,
        project_root,
    )

    assert threads == ["FIRST_VISIBLE_THREAD", "SECOND_VISIBLE_THREAD"]


def test_cached_open_threads_body_still_requires_matching_identity(
    monkeypatch,
    tmp_path: Path,
):
    import build_advisory

    project = tmp_path / "requested"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    state_path = tmp_path / "projects" / "requested" / "state.md"
    forged = (
        f"- Project root JSON: {json.dumps(str(other.resolve()))}\n"
        '- Runtime slug JSON: "requested"\n'
        "## Open threads\n"
        "- FORGED_CACHED_THREAD\n"
    )
    monkeypatch.setattr(
        build_advisory,
        "_read_trusted_state_body",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached validation must not reread state")
        ),
    )

    threads = build_advisory._read_open_threads(
        "requested",
        state_path,
        project,
        trusted_state_body=forged,
    )

    assert threads == []


@pytest.mark.parametrize(
    ("include_project_state", "expected_git_calls"),
    ((True, 5), (False, 0)),
    ids=("project-included", "project-omitted"),
)
def test_context_build_caches_project_freshness_and_omit_skips_git(
    monkeypatch,
    tmp_path: Path,
    include_project_state: bool,
    expected_git_calls: int,
):
    import build_advisory
    import session_start_context
    import session_start_project_state

    project = tmp_path / "work" / "cached-project"
    (project / ".git").mkdir(parents=True)
    executable = Path(sys.executable).resolve()
    git_resolutions = 0

    def resolve_git() -> Path:
        nonlocal git_resolutions
        git_resolutions += 1
        return executable

    monkeypatch.setattr(
        session_start_project_state,
        "_resolve_git_executable",
        resolve_git,
    )
    git_calls: list[list[str | Path]] = []

    def fake_git(command, **_kwargs):
        git_calls.append(list(command))
        output = str(project.resolve()) if "--show-toplevel" in command else head
        return session_start_project_state.BoundedProcessResult(0, output.encode(), b"")

    monkeypatch.setattr(session_start_project_state, "_run_bounded_process", fake_git)
    projects = tmp_path / "projects"
    state_path = projects / "cached-project" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Cached project state\n"
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "cached-project"\n'
        "## Where we left off\n"
        "CACHED_HANDOFF\n"
        "## Open threads\n"
        "- CACHED_THREAD\n",
        encoding="utf-8",
    )
    head = "a" * 40
    fingerprint = bootstrap_project._bootstrap_source_fingerprint(project, head)
    assert fingerprint is not None
    git_calls.clear()
    git_resolutions = 0
    state_path.with_name("bootstrap.md").write_text(
        "---\n"
        "type: bootstrap-context\n"
        'project_slug_json: "cached-project"\n'
        f"project_root_json: {json.dumps(str(project.resolve()))}\n"
        f"project_state_path_json: {json.dumps(str(state_path.resolve()))}\n"
        f"git_head_json: {json.dumps(head)}\n"
        "bootstrap_schema_json: 2\n"
        f"source_fingerprint_json: {json.dumps(fingerprint)}\n"
        "---\n\n"
        "# Cached bootstrap\n\nCACHED_BOOTSTRAP\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", tmp_path / "missing-daily")
    monkeypatch.setattr(session_start_context, "guardrails_block", lambda *_args: "")
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\nHEALTH",
    )
    monkeypatch.setattr(
        build_advisory,
        "_note_inventory",
        lambda: session_start_context.BoundedPathInventory(()),
    )
    monkeypatch.setattr(
        build_advisory,
        "_lint_report_inventory",
        lambda: session_start_context.BoundedPathInventory(()),
    )
    monkeypatch.setattr(build_advisory, "_find_stale_pages", lambda _inventory: 0)
    state_reads = 0
    real_state_read = session_start_project_state._read_trusted_state_body

    def counted_state_read(*args, **kwargs):
        nonlocal state_reads
        state_reads += 1
        return real_state_read(*args, **kwargs)

    monkeypatch.setattr(
        session_start_project_state,
        "_read_trusted_state_body",
        counted_state_read,
    )
    monkeypatch.setattr(
        session_start_context,
        "_read_trusted_state_body",
        counted_state_read,
    )
    monkeypatch.setattr(
        build_advisory,
        "_read_trusted_state_body",
        counted_state_read,
    )

    context = session_start_context.build_context(
        project,
        include_project_state=include_project_state,
    )

    assert len(git_calls) == expected_git_calls
    assert git_resolutions == (1 if include_project_state else 0)
    assert state_reads == 1
    assert ("## Current project state" in context) is include_project_state


def test_global_compile_health_uses_metadata_without_reading_daily_content(
    monkeypatch, tmp_path
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-01.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {daily.name: digest},
            "compiled_daily_receipts": {
                daily.name: _empty_effect_receipt(digest),
            },
        },
    )
    hash_calls: list[Path] = []

    def reject_hash(path: Path) -> str:
        hash_calls.append(path)
        raise AssertionError("SessionStart must not hash daily content")

    monkeypatch.setattr(session_start_context, "file_hash", reject_hash, raising=False)
    real_open = Path.open

    def reject_daily_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == daily and "r" in mode:
            raise AssertionError("SessionStart must not read daily content")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_daily_open)

    health = session_start_context.metacognitive_block()

    assert "**Global compile health**: up to date" in health
    assert hash_calls == []


def test_invalid_compiled_daily_hashes_never_report_up_to_date(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")

    for invalid in (None, "recorded", "A" * 64, "g" * 64, "a" * 63, "a" * 65):
        state = {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {daily.name: invalid},
        }

        status, details = session_start_context._compile_pending_state(state)

        assert status == "unknown"
        assert "invalid compiled daily hash" in "; ".join(details)


def test_compile_health_never_trusts_matching_hash_without_receipt(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    daily_dir = tmp_path / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    monkeypatch.setattr(session_start_context, "ROOT", tmp_path)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    state = {
        "last_compile_at": "2099-01-01T00:00:00",
        "compiled_daily_hashes": {daily.name: digest},
    }

    status, details = session_start_context._compile_pending_state(state)

    assert status == "pending"
    assert "uncompiled daily log" in "; ".join(details)


def test_global_compile_health_reports_pending_index_without_claiming_hash_change(
    monkeypatch, tmp_path
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("new content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {daily.name: digest},
            "compile_index_pending": {
                "batch_id": "a" * 64,
                "daily": daily.name,
                "sha256": digest,
                "generation_id": "b" * 64,
            },
        },
    )

    health = session_start_context.metacognitive_block()

    assert "**Global compile health**: pending" in health
    assert "index rebuild pending" in health
    assert "changed daily" not in health


@pytest.mark.parametrize(
    ("failure_state", "expected_detail"),
    (
        ({"last_index_rebuild_ok": False}, "last index rebuild failed"),
        ({"last_compile_status": "warning"}, "last compile completed with warnings"),
        ({"last_compile_status": "error"}, "last compile failed"),
    ),
)
def test_known_compile_or_index_failure_can_never_report_up_to_date(
    monkeypatch,
    tmp_path,
    failure_state,
    expected_detail,
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    state = {
        "last_compile_at": "2099-01-01T00:00:00",
        "compiled_daily_hashes": {daily.name: digest},
        **failure_state,
    }
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(session_start_context, "load_state", lambda: state)

    status, details = session_start_context._compile_pending_state(state)
    health = session_start_context.metacognitive_block()

    assert status == "pending"
    assert expected_detail in details
    assert "**Global compile health**: pending" in health
    assert "**Global compile health**: up to date" not in health


@pytest.mark.parametrize(
    ("invalid_metadata", "expected_detail"),
    (
        (
            {"last_compile_status": "unexpected-status"},
            "compile status metadata invalid",
        ),
        (
            {"last_index_rebuild_ok": "yes"},
            "index rebuild status metadata invalid",
        ),
    ),
)
def test_invalid_compile_health_metadata_never_reports_up_to_date(
    monkeypatch,
    tmp_path,
    invalid_metadata,
    expected_detail,
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    state = {
        "last_compile_at": "2099-01-01T00:00:00",
        "compiled_daily_hashes": {daily.name: digest},
        "compiled_daily_receipts": {
            daily.name: _empty_effect_receipt(digest),
        },
        **invalid_metadata,
    }
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(session_start_context, "load_state", lambda: state)

    status, details = session_start_context._compile_pending_state(state)
    health = session_start_context.metacognitive_block()

    assert status == "unknown"
    assert expected_detail in details
    assert "**Global compile health**: unknown" in health
    assert "**Global compile health**: up to date" not in health


def test_compile_pending_metadata_supports_producer_and_legacy_schema(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    index_pending = {
        "batch_id": "a" * 64,
        "daily": "2026-07-28.md",
        "sha256": "b" * 64,
        "generation_id": "c" * 64,
    }
    generation_active = {
        "2026-07-28.md": {
            "generation_id": "d" * 64,
            "source_sha256": "e" * 64,
        }
    }
    fields = (
        (
            "compile_index_pending",
            "index rebuild pending",
            "index pending metadata invalid",
            index_pending,
            (
                {"batch_id": "a" * 64},
                {**index_pending, "sha256": "invalid"},
                {**index_pending, "unexpected": True},
            ),
        ),
        (
            "compile_generation_active",
            "compile generation active",
            "compile generation metadata invalid",
            generation_active,
            (
                {"2026-07-28.md": {"generation_id": "d" * 64}},
                {"2026-07-28.md": "active"},
                {
                    **generation_active,
                    "broken.md": {
                        "generation_id": "f" * 64,
                        "source_sha256": "invalid",
                    },
                },
            ),
        ),
    )

    baseline = {
        "last_compile_at": "2099-01-01T00:00:00",
        "compiled_daily_hashes": {},
    }
    assert session_start_context._compile_pending_state(
        baseline,
        daily_inventory=session_start_context.BoundedPathInventory(()),
    )[0] == "up to date"

    for field, pending_detail, invalid_detail, producer_value, malformed in fields:
        values = (
            *((value, "up to date") for value in (False, {})),
            *((value, "pending") for value in (True, producer_value)),
            *((value, "unknown") for value in (None, 0, 1, "", [], *malformed)),
        )
        for value, expected_status in values:
            state = {
                **baseline,
                field: value,
            }

            status, details = session_start_context._compile_pending_state(
                state,
                daily_inventory=session_start_context.BoundedPathInventory(()),
            )

            assert status == expected_status, (field, value, details)
            if expected_status == "pending":
                assert pending_detail in details
            elif expected_status == "unknown":
                assert invalid_detail in details
            else:
                assert pending_detail not in details
                assert invalid_detail not in details


def test_global_compile_health_reports_uncompiled_daily_from_membership(
    monkeypatch, tmp_path
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-07-28.md").write_text("new content\n", encoding="utf-8")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {},
        },
    )

    health = session_start_context.metacognitive_block()

    assert "**Global compile health**: pending" in health
    assert "1 uncompiled daily log" in health


def test_global_compile_health_marks_post_compile_mtime_as_possibly_pending(
    monkeypatch, tmp_path
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2000-01-01T00:00:00",
            "compiled_daily_hashes": {daily.name: digest},
            "compiled_daily_receipts": {
                daily.name: _empty_effect_receipt(digest),
            },
        },
    )

    health = session_start_context.metacognitive_block()

    assert "**Global compile health**: possibly pending" in health
    assert "modified after the last successful compile" in health


def test_global_compile_health_is_unknown_without_success_timestamp(
    monkeypatch, tmp_path
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "compiled_daily_hashes": {daily.name: digest},
            "compiled_daily_receipts": {
                daily.name: _empty_effect_receipt(digest),
            },
        },
    )

    health = session_start_context.metacognitive_block()

    assert "**Global compile health**: unknown" in health
    assert "successful compile timestamp unavailable" in health


def test_global_compile_health_reports_active_generation_as_pending(
    monkeypatch, tmp_path
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    daily.write_text("compiled content\n", encoding="utf-8")
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {daily.name: digest},
            "compile_generation_active": {
                daily.name: {
                    "generation_id": "a" * 64,
                    "source_sha256": digest,
                }
            },
        },
    )

    health = session_start_context.metacognitive_block()

    assert "**Global compile health**: pending" in health
    assert "compile generation active" in health


def test_global_compile_health_detects_queue_without_reading_tasks(
    monkeypatch, tmp_path
):
    import session_start_context

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir(parents=True)
    task = queue_dir / "task.json"
    task.write_text('{"type":"compile"}\n', encoding="utf-8")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", queue_dir, raising=False)
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {},
        },
    )
    real_open = Path.open

    def reject_queue_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == task and "r" in mode:
            raise AssertionError("SessionStart must not read queued task content")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_queue_open)

    health = session_start_context.metacognitive_block()

    assert "**Global compile health**: possibly pending" in health
    assert "queue work outstanding" in health


def test_compile_health_bounds_all_queue_entries_before_suffix_filtering(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir(parents=True)
    for index in range(3):
        (queue_dir / f"irrelevant-{index}.tmp").write_text("ignored\n", encoding="utf-8")
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", queue_dir)
    monkeypatch.setattr(
        session_start_context,
        "MAX_INVENTORY_ENTRIES_SCANNED",
        2,
    )
    state = {
        "last_compile_at": "2099-01-01T00:00:00",
        "compiled_daily_hashes": {},
    }

    status, details = session_start_context._compile_pending_state(
        state,
        daily_inventory=session_start_context.BoundedPathInventory(()),
    )

    assert status == "unknown"
    assert "queue inventory exceeds the 2-entry work cap" in details


def test_compile_health_ignores_partial_queue_match_when_inventory_overflows(
    monkeypatch,
    tmp_path,
):
    import session_start_context

    queue_dir = tmp_path / "run" / "queue"
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", queue_dir)
    monkeypatch.setattr(
        session_start_context,
        "bounded_path_inventory",
        lambda *_args, **_kwargs: session_start_context.BoundedPathInventory(
            (queue_dir / "partial.json",),
            overflow=True,
        ),
    )
    state = {
        "last_compile_at": "2099-01-01T00:00:00",
        "compiled_daily_hashes": {},
    }

    status, details = session_start_context._compile_pending_state(
        state,
        daily_inventory=session_start_context.BoundedPathInventory(()),
    )

    assert status == "unknown"
    assert "queue work outstanding" not in details
    assert "queue inventory exceeds" in "; ".join(details)


def test_oversized_index_preserves_every_context_section(monkeypatch, tmp_path):
    import session_start_context

    index = tmp_path / "index.md"
    index.write_text(
        "# Oversized Index\n\n## Entry points\n"
        + "\n".join(f"- [[page-{i}]] " + "x" * 500 for i in range(100)),
        encoding="utf-8",
    )
    log = tmp_path / "log.md"
    log.write_text(
        "- 2026-07-25 - LOG_SENTINEL\n- 2026-07-26 - " + "l" * 1000,
        encoding="utf-8",
    )
    project_dir = tmp_path / "active-project"
    project_dir.mkdir()
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-07-25.md").write_text(
        "## [12:00:00] session-end | budget-session\n"
        "- Project slug: `active-project`\n"
        f"- Project root JSON: {json.dumps(str(project_dir.resolve()))}\n\n"
        "DAILY_SENTINEL " + "d" * 2000,
        encoding="utf-8",
    )
    projects_dir = tmp_path / "projects"
    state_dir = projects_dir / "active-project"
    state_dir.mkdir(parents=True)
    (state_dir / "state.md").write_text(
        "# Active project\n\nPROJECT_SENTINEL\n\n"
        f"- Project root JSON: {json.dumps(str(project_dir.resolve()))}\n"
        '- Runtime slug JSON: "active-project"\n'
        + "p" * 1000,
        encoding="utf-8",
    )

    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", index)
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", log)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects_dir, raising=False)
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda _slug=None, _project_root=None: (
            "## Guard rails\n\nGUARDRAIL_SENTINEL\n" + "g" * 1000
        ),
    )
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\nHEALTH_SENTINEL\n" + "h" * 1000,
    )
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "## Advisory\n\nADVISORY_SENTINEL\n" + "a" * 1000,
    )

    context = session_start_context.build_context(project_dir)

    assert 0 < len(context) <= session_start_context.MAX_CONTEXT_CHARS
    for expected in (
        "## Guard rails",
        "GUARDRAIL_SENTINEL",
        "## Your knowledge state (self-awareness)",
        "HEALTH_SENTINEL",
        "## Current project state",
        "PROJECT_SENTINEL",
        "## Advisory",
        "ADVISORY_SENTINEL",
        "## Latest daily log",
        "DAILY_SENTINEL",
        "## Recent knowledge/log.md",
        "LOG_SENTINEL",
        "## knowledge/index.md (trimmed)",
        "Oversized Index",
    ):
        assert expected in context


def test_unused_section_budget_expands_project_state_with_all_headings(monkeypatch, tmp_path):
    import session_start_context

    index = tmp_path / "index.md"
    index.write_text("# Small Index\n\n## Entry points\n- [[one]]\n", encoding="utf-8")
    log = tmp_path / "log.md"
    log.write_text("- 2026-07-28 - small log\n", encoding="utf-8")
    project_dir = tmp_path / "active-project"
    project_dir.mkdir()
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-07-28.md").write_text(
        "## [12:00:00] session-end | budget-session\n"
        "- Project slug: `active-project`\n"
        f"- Project root JSON: {json.dumps(str(project_dir.resolve()))}\n\n"
        "small daily summary\n",
        encoding="utf-8",
    )
    projects_dir = tmp_path / "projects"
    state_dir = projects_dir / "active-project"
    state_dir.mkdir(parents=True)
    state_lines = [f"- state line {index}: " + "p" * 32 for index in range(14)]
    (state_dir / "state.md").write_text(
        "# Active project\n\n"
        + "\n".join(state_lines)
        + "\nPROJECT_EXPANDED_SENTINEL\n\n"
        + f"- Project root JSON: {json.dumps(str(project_dir.resolve()))}\n"
        + '- Runtime slug JSON: "active-project"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", index)
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", log)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda _slug=None, _project_root=None: "## Guard rails\n\nsmall guardrail",
    )
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\nsmall health",
    )
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args, **_kwargs: "## Advisory\n\nsmall advisory",
    )

    context = session_start_context.build_context(project_dir)

    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS
    assert "PROJECT_EXPANDED_SENTINEL" in context
    for heading in (
        "## Guard rails",
        "## Your knowledge state (self-awareness)",
        "## Current project state",
        "## Advisory",
        "## Latest daily log",
        "## Recent knowledge/log.md",
        "## knowledge/index.md (trimmed)",
    ):
        assert heading in context


def test_context_root_resolution_keeps_nested_cwd_under_agent_root(tmp_path: Path):
    import session_start_context

    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)

    resolved = session_start_context._active_project_directory(
        None,
        stream=io.StringIO(json.dumps({"cwd": str(nested)})),
        env={"CLAUDE_PROJECT_DIR": str(project)},
    )

    assert resolved.root == project.resolve()
    assert resolved.signal_present is True


def test_active_but_unconfirmed_context_exposes_only_global_health_and_index(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    index = tmp_path / "index.md"
    index.write_text(
        "# INDEX_SENTINEL\n\n## Entry points\n- [[safe]]\n",
        encoding="utf-8",
    )
    log = tmp_path / "log.md"
    log.write_text("- 2026-07-28 - LOG_MUST_NOT_APPEAR\n", encoding="utf-8")
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-07-28.md").write_text(
        "## [10:00:00] legacy\nLEGACY_DAILY_MUST_NOT_APPEAR\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", index)
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", log)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily)
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _active: (None, None),
    )
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\nHEALTH_SENTINEL\n",
    )
    monkeypatch.setattr(
        session_start_context,
        "guardrails_block",
        lambda *_args: (_ for _ in ()).throw(AssertionError("guardrails read")),
    )
    monkeypatch.setattr(
        session_start_context,
        "advisory_block",
        lambda *_args: (_ for _ in ()).throw(AssertionError("advisory read")),
    )

    context = session_start_context.build_context(
        tmp_path / "unconfirmed-project",
        active_signal=True,
    )

    assert "HEALTH_SENTINEL" in context
    assert "INDEX_SENTINEL" in context
    for excluded in (
        "LEGACY_DAILY_MUST_NOT_APPEAR",
        "LOG_MUST_NOT_APPEAR",
        "## Guard rails",
        "## Current project state",
        "## Advisory",
        "## Latest daily log",
        "## Recent knowledge/log.md",
    ):
        assert excluded not in context


def test_output_file_without_directory_is_active_unconfirmed_global_only(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    index = tmp_path / "index.md"
    index.write_text(
        "# CACHE_INDEX_SENTINEL\n\n## Entry points\n- [[safe]]\n",
        encoding="utf-8",
    )
    log = tmp_path / "log.md"
    log.write_text("- 2026-07-28 - CACHE_LOG_MUST_NOT_APPEAR\n", encoding="utf-8")
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-07-28.md").write_text(
        "## [10:00:00] legacy\nCACHE_DAILY_MUST_NOT_APPEAR\n",
        encoding="utf-8",
    )
    output = tmp_path / "cache" / "session-context.md"
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", index)
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", log)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _active: (None, None),
    )
    monkeypatch.setattr(
        session_start_context,
        "metacognitive_block",
        lambda: "## Your knowledge state (self-awareness)\n\nCACHE_HEALTH_SENTINEL\n",
    )
    monkeypatch.setattr(session_start_context, "write_debug", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--output-file", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert session_start_context.main() == 0
    context = output.read_text(encoding="utf-8")

    assert "CACHE_HEALTH_SENTINEL" in context
    assert "CACHE_INDEX_SENTINEL" in context
    for excluded in (
        "CACHE_DAILY_MUST_NOT_APPEAR",
        "CACHE_LOG_MUST_NOT_APPEAR",
        "## Current project state",
        "## Latest daily log",
        "## Recent knowledge/log.md",
    ):
        assert excluded not in context


def test_session_start_inventory_overflow_reports_n_plus_and_stays_uncertain(
    monkeypatch,
    tmp_path: Path,
):
    import session_start_context

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    dailies = vault / "knowledge" / "daily"
    projects = vault / "knowledge" / "projects"
    skills = vault / "skills"
    gaps = vault / "gaps"
    queue = vault / "run" / "queue"
    for directory in (notes, dailies, projects, skills, gaps, queue):
        directory.mkdir(parents=True)
    for index in range(3):
        (notes / f"note-{index}.md").write_text("note\n", encoding="utf-8")
        (dailies / f"2026-07-{20 + index}.md").write_text(
            f"## [10:00:0{index}] legacy\nDAILY_{index}\n",
            encoding="utf-8",
        )
        state = projects / f"project-{index}" / "state.md"
        state.parent.mkdir()
        state.write_text("state\n", encoding="utf-8")
    monkeypatch.setattr(session_start_context, "ROOT", vault)
    monkeypatch.setattr(session_start_context, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", dailies)
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)
    monkeypatch.setattr(session_start_context, "SKILLS_DIR", skills)
    monkeypatch.setattr(session_start_context, "GAPS_DIR", gaps)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", queue)
    (vault / "knowledge" / "index.md").write_text("# Index\n", encoding="utf-8")
    monkeypatch.setattr(
        session_start_context,
        "MAX_INVENTORY_ENTRIES_SCANNED",
        2,
        raising=False,
    )
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in dailies.glob("*.md")
            },
            "compiled_daily_receipts": {
                path.name: _empty_effect_receipt(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    f"{index + 1:064x}",
                )
                for index, path in enumerate(dailies.glob("*.md"))
            },
        },
    )

    health = session_start_context.metacognitive_block()

    assert "2+ knowledge pages" in health
    assert "2+ daily logs" in health
    assert "2+ active project(s)" in health
    assert "**Global compile health**: unknown" in health
    assert "**Global compile health**: up to date" not in health
    assert session_start_context.latest_daily() is None


def test_session_start_inventory_errors_report_unknown_and_fail_safely(
    monkeypatch,
    tmp_path: Path,
):
    import memory_state
    import session_start_context

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    dailies = vault / "knowledge" / "daily"
    projects = vault / "knowledge" / "projects"
    skills = vault / "skills"
    gaps = vault / "gaps"
    queue = vault / "run" / "queue"
    for directory in (notes, dailies, projects, skills, gaps, queue):
        directory.mkdir(parents=True)
    monkeypatch.setattr(session_start_context, "ROOT", vault)
    monkeypatch.setattr(session_start_context, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(session_start_context, "DAILY_DIR", dailies)
    monkeypatch.setattr(session_start_context, "PROJECTS_DIR", projects)
    monkeypatch.setattr(session_start_context, "SKILLS_DIR", skills)
    monkeypatch.setattr(session_start_context, "GAPS_DIR", gaps)
    monkeypatch.setattr(session_start_context, "QUEUE_DIR", queue)
    monkeypatch.setattr(
        session_start_context,
        "load_state",
        lambda: {
            "last_compile_at": "2099-01-01T00:00:00",
            "compiled_daily_hashes": {},
        },
    )
    denied = {notes, dailies, projects}
    real_scandir = memory_state.os.scandir

    def denied_scandir(path):
        if Path(path) in denied:
            raise PermissionError(f"denied: {path}")
        return real_scandir(path)

    monkeypatch.setattr(memory_state.os, "scandir", denied_scandir)

    health = session_start_context.metacognitive_block()

    assert "unknown knowledge pages" in health
    assert "unknown daily logs" in health
    assert "unknown active project(s)" in health
    assert "**Global compile health**: unknown" in health
    assert "**Global compile health**: up to date" not in health
    assert session_start_context.latest_daily() is None

    denied.clear()
    monkeypatch.setattr(session_start_context, "DAILY_DIR", tmp_path / "missing-daily")
    monkeypatch.setattr(session_start_context, "GAPS_DIR", tmp_path / "missing-gaps")

    missing_root_health = session_start_context.metacognitive_block()

    assert "unknown daily logs" in missing_root_health
    assert "daily inventory unavailable" in missing_root_health
    assert "0 gaps" in missing_root_health
    assert "gaps inventory unavailable" not in missing_root_health
    assert "**Global compile health**: unknown" in missing_root_health
    assert "**Global compile health**: up to date" not in missing_root_health


def test_manual_context_uses_exact_registry_state_and_structured_daily_selector(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, daily, projects = _configure_manual_builder(monkeypatch, vault)
    app_root = tmp_path / "workspaces" / "app"
    other_root = tmp_path / "workspaces" / "my-app"
    _write_manual_builder_state(
        projects,
        "legacy physical folder",
        "app",
        app_root,
        "EXACT_STATE_HANDOFF",
    )
    _write_manual_builder_page(
        notes,
        "app",
        "app",
        "APP_KNOWLEDGE_SENTINEL",
        project_root=app_root,
    )
    _write_manual_builder_page(
        notes,
        "my-app",
        "my-app",
        "OTHER_PROJECT_KNOWLEDGE",
        project_root=other_root,
    )
    daily.mkdir(parents=True)
    (daily / "2026-07-28.md").write_text(
        "- `[09:00:00] tool | session | app | edit` "
        f"project-root-json={json.dumps(str(app_root.resolve()))} | "
        "TOOL_BREADCRUMB_SENTINEL\n"
        "- `[09:01:00] prompt | session | my-app` "
        f"project-root-json={json.dumps(str(other_root.resolve()))} | "
        "MY_APP_PROMPT_SENTINEL\n"
        "- `[09:02:00] prompt | session | app` "
        f"project-root-json={json.dumps(str(other_root.resolve()))} | "
        "WRONG_ROOT_PROMPT_SENTINEL\n"
        "- `[09:03:00] prompt | session | app` "
        f"project-root-json={json.dumps(str(app_root.resolve()))} | "
        "APP_PROMPT_SENTINEL\n",
        encoding="utf-8",
    )

    context = build_context.build_context("app", agent="opencode")

    assert "EXACT_STATE_HANDOFF" in context
    assert "APP_KNOWLEDGE_SENTINEL" in context
    assert "APP_PROMPT_SENTINEL" in context
    assert "OTHER_PROJECT_KNOWLEDGE" not in context
    assert "TOOL_BREADCRUMB_SENTINEL" not in context
    assert "MY_APP_PROMPT_SENTINEL" not in context
    assert "WRONG_ROOT_PROMPT_SENTINEL" not in context


def test_manual_context_marks_600_char_handoff_without_partial_source_lines(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    _notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspaces" / "bounded-handoff"
    first_prefix = "COMPLETE_600_CHAR_HANDOFF_"
    second_prefix = "OMITTED_600_CHAR_HANDOFF_"
    first_line = first_prefix + ("a" * (299 - len(first_prefix)))
    second_line = second_prefix + ("b" * (300 - len(second_prefix)))
    handoff = f"{first_line}\n{second_line}"
    assert len(handoff) == 600
    _write_manual_builder_state(
        projects,
        "bounded-handoff",
        "bounded-handoff",
        project_root,
        handoff,
    )

    context = build_context.build_context("bounded-handoff")

    assert f"- {first_line}" in context
    assert second_line not in context
    assert all(not line.startswith(second_prefix) for line in context.splitlines())
    assert "... (line truncated)" in context
    assert "... (section truncated)" in context


def test_manual_context_final_budget_marks_omitted_complete_source_line(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    _notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspaces" / "final-budget"
    complete_line = "FINAL_COMPLETE_SOURCE_LINE"
    omitted_prefix = "FINAL_OMITTED_SOURCE_LINE_"
    omitted_line = omitted_prefix + ("z" * 300)
    _write_manual_builder_state(
        projects,
        "final-budget",
        "final-budget",
        project_root,
        f"{complete_line}\n{omitted_line}",
    )

    context = build_context.build_context("final-budget", max_chars=170)

    assert len(context) <= 170
    assert f"- {complete_line}" in context
    assert omitted_line not in context
    assert all(not line.startswith(omitted_prefix) for line in context.splitlines())
    assert "... (line truncated)" in context
    assert "... (section truncated)" in context
    assert "... (truncated)" not in context


def test_manual_context_pages_require_same_slug_and_exact_confirmed_root(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspaces" / "app"
    other_root = tmp_path / "other" / "app"
    _write_manual_builder_state(
        projects,
        "app",
        "app",
        project_root,
        "EXACT_STATE_HANDOFF",
    )
    notes.mkdir(parents=True)
    for filename, root_line, sentinel in (
        (
            "exact-root",
            f"project_root: {json.dumps(str(project_root.resolve()))}\n",
            "EXACT_ROOT_PAGE",
        ),
        (
            "wrong-root",
            f"project_root: {json.dumps(str(other_root.resolve()))}\n",
            "WRONG_ROOT_SAME_SLUG_PAGE",
        ),
        ("missing-root", "", "MISSING_ROOT_SAME_SLUG_PAGE"),
    ):
        (notes / f"{filename}.md").write_text(
            "---\n"
            "type: pattern\n"
            "project: app\n"
            f"{root_line}"
            "status: active\n"
            "---\n"
            f"# {filename}\n"
            f"One-sentence summary: {sentinel}\n",
            encoding="utf-8",
        )
    daily.mkdir(parents=True)

    context = build_context.build_context("app")

    assert "EXACT_ROOT_PAGE" in context
    assert "WRONG_ROOT_SAME_SLUG_PAGE" not in context
    assert "MISSING_ROOT_SAME_SLUG_PAGE" not in context


@pytest.mark.parametrize("record_root", (None, "other"))
def test_manual_context_heartbeat_requires_exact_confirmed_root(
    monkeypatch,
    tmp_path: Path,
    record_root: str | None,
):
    vault = tmp_path / "vault"
    _notes, daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspaces" / "app"
    other_root = tmp_path / "other" / "app"
    _write_manual_builder_state(
        projects,
        "app",
        "app",
        project_root,
        "EXACT_STATE_HANDOFF",
    )
    heartbeat = {
        "at": "2026-08-02T12:00:00",
        "reason": "CROSS_ROOT_HEARTBEAT_MUST_NOT_APPEAR",
    }
    if record_root == "other":
        heartbeat["project_root"] = str(other_root.resolve())
    monkeypatch.setattr(
        build_context,
        "load_state",
        lambda: {"codex_heartbeats": {"app": heartbeat}},
    )
    daily.mkdir(parents=True)

    context = build_context.build_context("app")

    assert "CROSS_ROOT_HEARTBEAT_MUST_NOT_APPEAR" not in context
    assert "### Last seen" not in context


def test_manual_context_rejects_legacy_only_state_ownership(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspaces" / "bank-list"
    project_root.mkdir(parents=True)
    state_path = projects / "bank-list" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Bank list state\n"
        f"- Project root: `{project_root.resolve()}`\n"
        '- Runtime slug JSON: "bank-list"\n'
        "## Where we left off\nLEGACY_BANK_LIST_HANDOFF\n",
        encoding="utf-8",
    )
    _write_manual_builder_page(notes, "bank-list", "bank-list", "BANK_LIST_PAGE")
    daily.mkdir(parents=True)

    context = build_context.build_context("bank-list")

    assert "LEGACY_BANK_LIST_HANDOFF" not in context
    assert "BANK_LIST_PAGE" not in context


def test_manual_context_orders_handoff_then_current_bootstrap_then_daily(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    _notes, daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspaces" / "ordered"
    state_path = _write_manual_builder_state(
        projects,
        "ordered",
        "ordered",
        project_root,
        "FIRST_SAVED_HANDOFF",
    )
    fingerprint = bootstrap_project._bootstrap_source_fingerprint(project_root, None)
    assert fingerprint is not None
    state_path.with_name("bootstrap.md").write_text(
        "---\n"
        "type: bootstrap-context\n"
        'project_slug_json: "ordered"\n'
        f"project_root_json: {json.dumps(str(project_root.resolve()))}\n"
        f"project_state_path_json: {json.dumps(str(state_path.resolve()))}\n"
        "git_head_json: null\n"
        "bootstrap_schema_json: 2\n"
        f"source_fingerprint_json: {json.dumps(fingerprint)}\n"
        "---\n\n"
        "# Ordered bootstrap\n\nSECOND_CURRENT_BOOTSTRAP\n",
        encoding="utf-8",
    )
    daily.mkdir(parents=True)
    (daily / "2026-08-02.md").write_text(
        "- `[12:00:00] prompt | session | ordered` "
        f"project-root-json={json.dumps(str(project_root.resolve()))} | "
        "THIRD_RECENT_DAILY\n",
        encoding="utf-8",
    )

    context = build_context.build_context("ordered")

    assert "Project bootstrap (UNTRUSTED project-derived data)" in context
    assert context.index("FIRST_SAVED_HANDOFF") < context.index(
        "SECOND_CURRENT_BOOTSTRAP"
    ) < context.index("THIRD_RECENT_DAILY")


def test_manual_context_write_targets_exact_state_parent(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    _notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    state_path = _write_manual_builder_state(
        projects,
        "legacy physical folder",
        "app",
        tmp_path / "workspaces" / "app",
        "WRITE_TARGET_HANDOFF",
    )
    monkeypatch.setattr(sys, "argv", ["build_context.py", "app", "--write"])

    assert build_context.main() == 0
    assert (state_path.parent / "context.md").is_file()
    assert not (projects / "app" / "context.md").exists()


def test_manual_context_write_fails_closed_for_missing_or_ambiguous_alias(
    monkeypatch,
    tmp_path: Path,
):
    for case in ("missing", "ambiguous"):
        vault = tmp_path / case / "vault"
        _notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
        if case == "ambiguous":
            _write_manual_builder_state(
                projects,
                "legacy-a",
                "app",
                tmp_path / case / "workspace-a",
                "FIRST",
            )
            _write_manual_builder_state(
                projects,
                "legacy-b",
                "App",
                tmp_path / case / "workspace-b",
                "SECOND",
            )
        monkeypatch.setattr(sys, "argv", ["build_context.py", "app", "--write"])

        assert build_context.main() == 1
        assert list(projects.glob("*/context.md")) == []


def test_manual_context_omits_knowledge_when_bounded_inventory_overflows(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    _write_manual_builder_state(
        projects,
        "physical-app",
        "app",
        tmp_path / "workspace" / "app",
        "STATE_SURVIVES_NOTE_OVERFLOW",
    )
    _write_manual_builder_page(notes, "app", "app", "OVERFLOWED_KNOWLEDGE")
    _write_manual_builder_page(notes, "other", "other", "SECOND_PAGE")
    monkeypatch.setattr(build_context, "MAX_KNOWLEDGE_ENTRIES", 1, raising=False)

    context = build_context.build_context("app")

    assert "STATE_SURVIVES_NOTE_OVERFLOW" in context
    assert "OVERFLOWED_KNOWLEDGE" not in context


def test_manual_context_excludes_page_with_ambiguous_duplicate_status(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    _write_manual_builder_state(
        projects,
        "physical-app",
        "app",
        tmp_path / "workspace" / "app",
        "STATE_HANDOFF",
    )
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "ambiguous-status.md").write_text(
        "---\n"
        "type: pattern\n"
        "project: app\n"
        "status: active\n"
        "status: archived\n"
        "---\n"
        "# Ambiguous status\n"
        "One-sentence summary: AMBIGUOUS_STATUS_MUST_NOT_APPEAR\n",
        encoding="utf-8",
    )

    context = build_context.build_context("app")

    assert "STATE_HANDOFF" in context
    assert "AMBIGUOUS_STATUS_MUST_NOT_APPEAR" not in context


def test_manual_context_excludes_invalid_utf8_that_forges_project_scope(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    _write_manual_builder_state(
        projects,
        "physical-app",
        "app",
        tmp_path / "workspace" / "app",
        "STATE_HANDOFF",
    )
    notes.mkdir(parents=True, exist_ok=True)
    page = notes / "invalid-utf8-scope.md"
    raw = (
        b"---\n"
        b"type: pattern\n"
        b"pro\xffject: app\n"
        b"status: active\n"
        b"---\n"
        b"# Invalid UTF-8 scope\n"
        b"One-sentence summary: INVALID_UTF8_SCOPE_MUST_NOT_APPEAR\n"
    )
    page.write_bytes(raw)

    assert "project: app" in raw.decode("utf-8", errors="ignore")
    context = build_context.build_context("app")

    assert "STATE_HANDOFF" in context
    assert "INVALID_UTF8_SCOPE_MUST_NOT_APPEAR" not in context
    assert build_context._read_text_bounded(page, build_context.MAX_NOTE_BYTES) is None


def test_manual_context_reads_each_knowledge_page_with_named_byte_limit(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspace" / "app"
    _write_manual_builder_state(
        projects,
        "physical-app",
        "app",
        project_root,
        "BOUNDED_READ_HANDOFF",
    )
    _write_manual_builder_page(
        notes,
        "app",
        "app",
        "BOUNDED_NOTE",
        project_root=project_root,
    )
    monkeypatch.setattr(build_context, "MAX_NOTE_BYTES", 256, raising=False)
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
        return TrackingFile(handle) if path.parent == notes else handle

    monkeypatch.setattr(Path, "open", tracking_open)

    build_context.build_context("app", agent="opencode")

    assert read_sizes
    assert all(0 < size <= build_context.MAX_NOTE_BYTES + 1 for size in read_sizes)


def test_manual_context_emits_type_groups_in_agent_priority_order(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    notes, _daily, projects = _configure_manual_builder(monkeypatch, vault)
    project_root = tmp_path / "workspace" / "app"
    _write_manual_builder_state(
        projects,
        "physical-app",
        "app",
        project_root,
        "AGENT_PRIORITY_HANDOFF",
    )
    _write_manual_builder_page(
        notes,
        "decision-page",
        "app",
        "DECISION_PAGE",
        project_root=project_root,
        page_type="decision",
    )
    _write_manual_builder_page(
        notes,
        "pattern-one",
        "app",
        "PATTERN_ONE",
        project_root=project_root,
    )
    _write_manual_builder_page(
        notes,
        "pattern-two",
        "app",
        "PATTERN_TWO",
        project_root=project_root,
    )

    context = build_context.build_context("app", agent="opencode")

    assert context.index("**patterns:**") < context.index("**decisions:**")
    assert "PATTERN_ONE" in context
    assert "PATTERN_TWO" in context
    assert "DECISION_PAGE" in context
