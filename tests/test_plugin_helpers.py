"""Phase 5 tests: OpenCode plugin's Python file-IO helpers.

Locks in:
1. Each helper exits 0 on empty/malformed stdin (never breaks plugin).
2. Each helper writes the expected artifact (daily log line / state heartbeat).
3. Each helper is idempotent across multiple calls within the same day.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _reload(module_name: str):
    """Force re-import so module-level env reads pick up monkeypatched env."""
    if module_name in sys.modules:
        del sys.modules[module_name]
    return __import__(module_name)


def _run_with_stdin(module_name: str, stdin_text: str) -> int:
    mod = _reload(module_name)
    with patch.object(sys, "stdin", io.StringIO(stdin_text)):
        return mod.main()


class _BoundedOnlyInput(io.StringIO):
    def __init__(self, value: str):
        super().__init__(value)
        self.request_sizes: list[int] = []

    def read(self, size: int = -1) -> str:
        self.request_sizes.append(size)
        assert size > 0, "reader requested an unbounded allocation"
        return super().read(size)


def _write_owned_state(vault: Path, project: Path, slug: str) -> None:
    project.mkdir(parents=True, exist_ok=True)
    state_path = vault / "knowledge" / "projects" / slug / "state.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        f"- Runtime slug JSON: {json.dumps(slug)}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "module_name",
    ("daily_log_append", "heartbeat_record", "tool_breadcrumb_append"),
)
@pytest.mark.parametrize("root_case", ("relative", "conflicting"))
def test_plugin_helpers_reject_relative_or_conflicting_project_roots(
    module_name,
    root_case,
    tmp_path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    project = tmp_path / "workspace" / "service"
    other = tmp_path / "other"
    other.mkdir()
    _write_owned_state(vault, project, "service")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.chdir(tmp_path)

    import memory_state

    state_root = tmp_path / "runtime"
    state_dir = state_root / "run"
    state_file = state_dir / "state.json"
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    import session_start_project_state

    def reject_confirmation(*_args, **_kwargs):
        raise AssertionError("invalid root must be rejected before confirmation")

    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        reject_confirmation,
    )

    raw_root = (
        str(project.relative_to(tmp_path))
        if root_case == "relative"
        else str(project.resolve())
    )
    payload = {
        "slug": "service",
        "projectRoot": raw_root,
        "sessionId": "session-1234",
        "tool": "edit",
        "target": "src/app.py",
        "block": (
            "## [10:00:00] test | opencode\n"
            "- Project slug: `service`\n"
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n\n"
            "UNTRUSTED_ROOT_MUST_NOT_WRITE\n"
        ),
    }
    if root_case == "conflicting":
        payload["project_dir"] = str(other.resolve())

    assert _run_with_stdin(module_name, json.dumps(payload)) == 0
    daily_dir = vault / "knowledge" / "daily"
    assert not daily_dir.exists() or list(daily_dir.glob("*.md")) == []
    assert not state_file.exists()


@pytest.mark.parametrize(
    "module_name",
    ("daily_log_append", "heartbeat_record", "tool_breadcrumb_append"),
)
def test_plugin_helpers_fail_silently_on_nul_vault_root(
    module_name,
    tmp_path,
    monkeypatch,
    capsys,
):
    project = tmp_path / "workspace" / "service"
    project.mkdir(parents=True)
    monkeypatch.setattr(
        os,
        "environ",
        {
            **os.environ,
            "LLM_WIKI_ROOT": f"{tmp_path / 'vault'}\0suffix",
        },
    )
    payload = {
        "slug": "service",
        "projectRoot": str(project.resolve()),
        "sessionId": "session-1234",
        "tool": "edit",
        "target": "src/app.py",
        "block": (
            "## [10:00:00] test | opencode\n"
            "- Project slug: `service`\n"
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n\n"
            "NUL_ROOT_MUST_NOT_WRITE\n"
        ),
    }

    assert _run_with_stdin(module_name, json.dumps(payload)) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "module_name",
    ("daily_log_append", "heartbeat_record", "tool_breadcrumb_append"),
)
def test_plugin_helpers_accept_legacy_alias_spelling_after_identity_migration(
    module_name,
    tmp_path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    project = tmp_path / "workspace" / "service"
    _write_owned_state(vault, project, "Service")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))

    import memory_state

    state_root = tmp_path / "runtime"
    state_dir = state_root / "run"
    state_file = state_dir / "state.json"
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")

    payload = {
        "slug": "Service",
        "projectRoot": str(project.resolve()),
        "sessionId": "session-1234",
        "tool": "edit",
        "target": "src/app.py",
        "block": (
            "## [10:00:00] opencode-idle | session-1234\n"
            "- Project slug: `Service`\n"
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
            "- Tier: `minor`\n"
            "- Source session: `session-1234`\n\n"
            "**Gotchas / debugging**\n"
            "- LEGACY_ALIAS_PAYLOAD fails unless identity aliases remain compatible.\n"
            "<!-- llm-wiki-record-complete -->\n"
            if module_name == "daily_log_append"
            else (
                "## [10:00:00] test | opencode\n"
                "- Project slug: `Service`\n"
                f"- Project root JSON: {json.dumps(str(project.resolve()))}\n\n"
                "LEGACY_ALIAS_PAYLOAD\n"
            )
        ),
    }

    assert _run_with_stdin(module_name, json.dumps(payload)) == 0
    if module_name == "heartbeat_record":
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert "service" in state["codex_heartbeats"]
    else:
        daily_files = list((vault / "knowledge" / "daily").glob("*.md"))
        assert len(daily_files) == 1


# ---------------------------------------------------------------------------
# daily_log_append.py
# ---------------------------------------------------------------------------


def test_daily_log_append_exits_zero_on_empty_stdin(capsys):
    assert _run_with_stdin("daily_log_append", "") == 0
    assert capsys.readouterr().out == ""


def test_daily_log_append_exits_zero_on_malformed_json(capsys):
    assert _run_with_stdin("daily_log_append", "not json {{{") == 0
    assert capsys.readouterr().out == ""


def test_daily_log_append_rejects_oversized_stdin_with_bounded_read(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    project = tmp_path.parent / f"{tmp_path.name}-project"
    _write_owned_state(tmp_path, project, "your-app")
    payload = json.dumps(
        {
            "slug": "your-app",
            "projectRoot": str(project),
            "sessionId": "opencode-abc123",
            "unused": "x" * 20_000,
            "block": (
                "## [10:00:00] test | opencode\n"
                "- Project slug: `your-app`\n"
                f"- Project root JSON: {json.dumps(str(project.resolve()))}\n\n"
                "OVERSIZED_STDIN_MUST_NOT_WRITE\n"
            ),
        }
    )
    module = _reload("daily_log_append")
    monkeypatch.setattr(module, "MAX_STDIN_BYTES", 1024, raising=False)
    read_sizes: list[int] = []

    class TrackingInput(io.StringIO):
        def read(self, size=-1):
            read_sizes.append(size)
            return super().read(size)

    with patch.object(sys, "stdin", TrackingInput(payload)):
        assert module.main() == 0

    assert read_sizes and all(size > 0 for size in read_sizes)
    assert list((tmp_path / "knowledge" / "daily").glob("*.md")) == []


def test_daily_log_append_rejects_lone_surrogate_in_ignored_nested_field(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    project = tmp_path.parent / f"{tmp_path.name}-project"
    _write_owned_state(tmp_path, project, "your-app")
    payload = {
        "slug": "your-app",
        "projectRoot": str(project),
        "sessionId": "opencode-abc123",
        "ignored": {"nested": [chr(0xD800)]},
        "block": (
            "## [10:00:00] test | opencode\n"
            "- Project slug: `your-app`\n"
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n\n"
            "SURROGATE_STDIN_MUST_NOT_WRITE\n"
        ),
    }

    assert _run_with_stdin("daily_log_append", json.dumps(payload)) == 0
    daily_dir = tmp_path / "knowledge" / "daily"
    assert not daily_dir.exists() or list(daily_dir.glob("*.md")) == []


def test_daily_log_append_rejects_deep_json_nesting(monkeypatch):
    depth = sys.getrecursionlimit() + 100
    payload = '{"ignored":' + "[" * depth + "null" + "]" * depth + "}"

    module = _reload("daily_log_append")
    appended: list[tuple] = []
    monkeypatch.setattr(module, "append_daily", lambda *args: appended.append(args))
    with patch.object(sys, "stdin", io.StringIO(payload)):
        assert module.main() == 0

    assert appended == []


def test_daily_log_append_writes_canonical_root_metadata(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    project = tmp_path.parent / f"{tmp_path.name}-project"
    _write_owned_state(tmp_path, project, "your-app")
    noncanonical_root = project.parent / "unused" / ".." / project.name
    payload = {
        "slug": "your-app",
        "projectRoot": str(project),
        "sessionId": "opencode-abc123",
        "block": (
            "## [10:00:00] opencode-idle | opencode-abc123\n"
            "- Project slug: `your-app`\n"
            f"- Project root JSON: {json.dumps(str(noncanonical_root))}\n"
            "- Tier: `major`\n"
            "- Source session: `opencode-abc123`\n\n"
            "**Lessons / patterns**\n"
            "- Always preserve canonical project metadata before append.\n"
            "<!-- llm-wiki-record-complete -->\n"
        ),
    }
    rc = _run_with_stdin("daily_log_append", json.dumps(payload))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "status": "appended",
    }

    today = date.today().isoformat()
    daily = tmp_path / "knowledge" / "daily" / f"{today}.md"
    assert daily.exists()
    content = daily.read_text(encoding="utf-8")
    assert "Tier: `major`" in content
    assert "Always preserve canonical project metadata before append." in content
    canonical_line = f"- Project root JSON: {json.dumps(str(project.resolve()))}"
    assert canonical_line in content
    assert str(noncanonical_root) not in content


@pytest.mark.parametrize(
    "case",
    (
        "incomplete",
        "invalid-tier",
        "minor-with-major-section",
        "session-mismatch",
        "unknown-session",
    ),
)
def test_daily_log_append_acks_only_valid_complete_classified_record(
    tmp_path,
    monkeypatch,
    capsys,
    case,
):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    project = tmp_path.parent / f"{tmp_path.name}-project"
    _write_owned_state(tmp_path, project, "your-app")
    session_id = "opencode-abc123"
    tier = "major"
    section = "Lessons / patterns"
    completion = "<!-- llm-wiki-record-complete -->\n"
    if case == "incomplete":
        completion = ""
    elif case == "invalid-tier":
        tier = "ok"
    elif case == "minor-with-major-section":
        tier = "minor"
    elif case == "session-mismatch":
        session_id = "different-session"
    elif case == "unknown-session":
        session_id = "unknown"

    source_session = "unknown" if case == "unknown-session" else "opencode-abc123"
    payload = {
        "slug": "your-app",
        "projectRoot": str(project),
        "sessionId": session_id,
        "block": (
            "## [10:00:00] opencode-idle | opencode-abc123\n"
            "- Project slug: `your-app`\n"
            f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
            f"- Tier: `{tier}`\n"
            f"- Source session: `{source_session}`\n\n"
            f"**{section}**\n"
            "- Always reject an invalid direct capture frame before append.\n"
            f"{completion}"
        ),
    }

    assert _run_with_stdin("daily_log_append", json.dumps(payload)) == 0
    assert capsys.readouterr().out == ""
    daily_dir = tmp_path / "knowledge" / "daily"
    assert not daily_dir.exists() or list(daily_dir.glob("*.md")) == []


def test_daily_log_append_rejects_scope_that_contradicts_confirmed_root(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    project = tmp_path.parent / f"{tmp_path.name}-project"
    other = tmp_path.parent / f"{tmp_path.name}-other"
    other.mkdir()
    _write_owned_state(tmp_path, project, "your-app")
    payload = {
        "slug": "your-app",
        "projectRoot": str(project),
        "sessionId": "opencode-abc123",
        "block": (
            "## [10:00:00] test | opencode\n"
            "- Project slug: `your-app`\n"
            f"- Project root JSON: {json.dumps(str(other.resolve()))}\n\n"
            "CROSS_PROJECT_CONTENT\n"
        ),
    }

    assert _run_with_stdin("daily_log_append", json.dumps(payload)) == 0
    assert list((tmp_path / "knowledge" / "daily").glob("*.md")) == []


def test_locked_append_once_is_atomic_across_concurrent_writers(tmp_path):
    import daily_log_append

    state_root = tmp_path / "state"
    daily = tmp_path / "knowledge" / "daily" / "2026-07-27.md"
    marker = f"<!-- llm-wiki-queue-task: {'a' * 64} -->"
    block = f"\n## deferred\n{marker}\n\nwrite exactly once\n"
    barrier = threading.Barrier(12)
    results = []
    errors = []

    def writer():
        try:
            barrier.wait()
            results.append(
                daily_log_append.locked_append_once(
                    daily,
                    block,
                    marker,
                    state_root=state_root,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert not [thread for thread in threads if thread.is_alive()]
    assert results == [daily] * 12
    text = daily.read_text(encoding="utf-8")
    assert text.count(marker) == 1
    assert text.count("write exactly once") == 1


@pytest.mark.parametrize("invalid_argument", ("daily_path", "state_root"))
def test_locked_append_rejects_nul_path_before_filesystem_mutation(
    tmp_path,
    invalid_argument,
):
    import daily_log_append

    daily_path = tmp_path / "daily" / "2026-07-28.md"
    state_root = tmp_path / "runtime"
    if invalid_argument == "daily_path":
        daily_path = Path(f"{daily_path}\0suffix")
    else:
        state_root = Path(f"{state_root}\0suffix")

    with pytest.raises(ValueError, match="contains NUL"):
        daily_log_append.locked_append(
            daily_path,
            "must not write\n",
            state_root=state_root,
        )

    assert not (tmp_path / "daily").exists()
    assert not (tmp_path / "runtime").exists()


# ---------------------------------------------------------------------------
# heartbeat_record.py
# ---------------------------------------------------------------------------


def test_heartbeat_record_exits_zero_on_empty_stdin():
    assert _run_with_stdin("heartbeat_record", "") == 0


def test_heartbeat_record_exits_zero_on_malformed_json_or_root(tmp_path, monkeypatch):
    assert _run_with_stdin("heartbeat_record", "{not json") == 0
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    payload = {"slug": "invalid", "projectRoot": "\0"}
    assert _run_with_stdin("heartbeat_record", json.dumps(payload)) == 0


def test_heartbeat_record_rejects_oversized_stdin_before_state_mutation(
    tmp_path,
    monkeypatch,
):
    import heartbeat_record
    import memory_state
    import session_start_project_state

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    project.mkdir()
    mutations: list[dict] = []
    payload = json.dumps(
        {
            "slug": "project",
            "projectRoot": str(project),
            "reason": "session-start",
            "sessionId": "session-oversized",
            "padding": "x" * 256,
        }
    )
    stream = _BoundedOnlyInput(payload)

    def track_update(mutator):
        state: dict = {}
        mutator(state)
        mutations.append(state)
        return state

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(heartbeat_record, "MAX_HOOK_STDIN_BYTES", 64, raising=False)
    monkeypatch.setattr(memory_state, "update_state", track_update)
    monkeypatch.setattr(
        session_start_project_state,
        "resolve_project_root",
        lambda *_args, **_kwargs: type("Resolution", (), {"root": project})(),
    )
    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        lambda *_args, **_kwargs: ("project", project),
    )
    with patch.object(sys, "stdin", stream):
        assert heartbeat_record.main() == 0

    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert mutations == []


def test_heartbeat_record_requires_confirmed_identity(tmp_path, monkeypatch):
    # memory_state caches STATE_ROOT at module-load time, so we patch
    # the resolved attributes directly rather than relying on env vars.
    vault = tmp_path / "vault"
    project = tmp_path / "test-project"
    _write_owned_state(vault, project, "test-project")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    fake_state_root = tmp_path / "runtime"
    fake_state_dir = fake_state_root / "run"
    fake_state_dir.mkdir(parents=True, exist_ok=True)
    fake_state_file = fake_state_dir / "state.json"

    # Patch memory_state module attributes (heartbeat_record imports
    # update_state from memory_state, which references STATE_FILE etc).
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_ROOT", fake_state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", fake_state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", fake_state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", fake_state_dir / "state.json.lock")

    invalid_payload = {
        "slug": "other-project",
        "projectRoot": str(project),
        "reason": "opencode-session-start",
        "sessionId": "opc-123",
    }
    assert _run_with_stdin("heartbeat_record", json.dumps(invalid_payload)) == 0
    assert not fake_state_file.exists()

    payload = {**invalid_payload, "slug": "test-project"}
    assert _run_with_stdin("heartbeat_record", json.dumps(payload)) == 0
    assert fake_state_file.exists()
    state = json.loads(fake_state_file.read_text(encoding="utf-8"))
    assert "test-project" in state.get("codex_heartbeats", {})
    hb = state["codex_heartbeats"]["test-project"]
    assert hb["reason"] == "opencode-session-start"
    assert hb["session_id"] == "opc-123"
    assert hb["source"] == "opencode"


# ---------------------------------------------------------------------------
# tool_breadcrumb_append.py
# ---------------------------------------------------------------------------


def test_tool_breadcrumb_exits_zero_on_empty_stdin():
    assert _run_with_stdin("tool_breadcrumb_append", "") == 0


def test_tool_breadcrumb_exits_zero_on_malformed_json():
    assert _run_with_stdin("tool_breadcrumb_append", "garbage") == 0


def test_tool_breadcrumb_rejects_oversized_stdin_before_append(
    tmp_path,
    monkeypatch,
):
    import daily_log_append
    import session_start_project_state
    import tool_breadcrumb_append

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    project.mkdir()
    appended: list[tuple] = []
    payload = json.dumps(
        {
            "slug": "project",
            "projectRoot": str(project),
            "sessionId": "session-oversized",
            "tool": "edit",
            "target": "src/app.py",
            "padding": "x" * 256,
        }
    )
    stream = _BoundedOnlyInput(payload)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(tool_breadcrumb_append, "MAX_HOOK_STDIN_BYTES", 64, raising=False)
    monkeypatch.setattr(daily_log_append, "append_daily", lambda *args: appended.append(args))
    monkeypatch.setattr(
        session_start_project_state,
        "resolve_project_root",
        lambda *_args, **_kwargs: type("Resolution", (), {"root": project})(),
    )
    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        lambda *_args, **_kwargs: ("project", project),
    )
    with patch.object(sys, "stdin", stream):
        assert tool_breadcrumb_append.main() == 0

    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert appended == []


def test_tool_breadcrumb_writes_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    project = tmp_path.parent / f"{tmp_path.name}-project"
    _write_owned_state(tmp_path, project, "your-app")
    payload = {
        "slug": "your-app",
        "projectRoot": str(project),
        "sessionId": "abcdefghij",
        "tool": "edit",
        "target": "src/auth.ts",
    }
    rc = _run_with_stdin("tool_breadcrumb_append", json.dumps(payload))
    assert rc == 0

    today = date.today().isoformat()
    daily = tmp_path / "knowledge" / "daily" / f"{today}.md"
    assert daily.exists()
    content = daily.read_text(encoding="utf-8")
    assert "tool" in content
    assert "your-app" in content
    assert "edit" in content
    assert "src/auth.ts" in content
    assert "abcdefgh" in content  # session_id truncated to 8 chars
