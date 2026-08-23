from __future__ import annotations

import concurrent.futures
import hashlib
import importlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

WRITER_TARGETS = [
    ("knowledge/daily/2026-07-15.md", b"daily"),
    ("knowledge/notes/page.md", b"note"),
    ("knowledge/projects/demo/state.md", b"project"),
    ("knowledge/inbox/source.md", b"inbox"),
    ("knowledge/feedback/abcdef123456.json", b"{}"),
    ("knowledge/index.md", b"index"),
    ("knowledge/log.md", b"log"),
    ("knowledge/guardrails.md", b"guardrails"),
    ("knowledge/projects/demo/.blackboard/tasks.jsonl", b"{}\n"),
]

TASK14_BEHAVIORAL_ENTRYPOINTS = {
    "scripts/access_tracking.py:flush_access_to_frontmatter",
    "scripts/archive_stale.py:_archive_page",
    "scripts/blackboard.py:_append_jsonl",
    "scripts/bootstrap_project.py:bootstrap",
    "scripts/build_guardrails.py:main",
    "scripts/build_context.py:main",
    "scripts/daily_log_append.py:locked_append",
    "scripts/daily_log_append.py:locked_append_once",
    "scripts/feedback_capture.py:capture_from_text",
    "scripts/feedback_capture.py:promote_candidate",
    "scripts/flush_memory.py:append_daily",
    "scripts/migrate_to_okf.py:_write_page",
    "scripts/query_memory.py:append_log",
    "scripts/query_memory.py:file_back",
    "scripts/rebuild_memory_index.py:main",
    "scripts/reflection.py:reflect_page",
    "scripts/session_end_project_tag.py:_append_entry",
    "scripts/session_start_project_state.py:main",
    "scripts/tool_breadcrumb_append.py:_append_breadcrumb",
    "scripts/user_prompt_capture.py:_append_prompt_tag",
}

TASK14_READ_TRANSFORM_WRITE_ENTRYPOINTS = {
    "scripts/access_tracking.py:flush_access_to_frontmatter",
    "scripts/archive_stale.py:_archive_page",
    "scripts/build_guardrails.py:main",
    "scripts/feedback_capture.py:promote_candidate",
    "scripts/migrate_to_okf.py:_write_page",
    "scripts/rebuild_memory_index.py:main",
    "scripts/reflection.py:reflect_page",
}


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes").mkdir()
    (vault / "knowledge" / "projects").mkdir()
    (vault / "knowledge" / "inbox").mkdir()
    return vault, state


def _is_contention(error: BaseException) -> bool:
    """Losing the writer gate, its lease, or the SQLite lock is contention."""
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, sqlite3.OperationalError):
        return "database is locked" in str(error)
    return isinstance(error, RuntimeError) and "gate ownership was lost" in str(error)


def _under_contention(call, *arguments, attempts: int = 12, **keywords):
    """Retry a write that lost the global writer gate rather than a race.

    The gate budget is ten seconds and a caller cannot extend it; the documented
    answer to losing it is to try again. Six processes appending on a hosted
    Windows runner, where every append also hardens files through `icacls`,
    reach that budget, exhaust the SQLite busy timeout, or outlive the gate
    lease. The operation id makes the retry converge on the same committed
    append, so none of that is a correctness signal.
    """
    for attempt in range(attempts):
        try:
            return call(*arguments, **keywords)
        except BaseException as error:
            if attempt == attempts - 1 or not _is_contention(error):
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _bounded_workers(requested: int) -> int:
    """Never ask for more concurrent writers than the machine can schedule.

    A four-core hosted runner given eight writer processes spends its time in
    the gate rather than in the code under test, and the starvation that
    produces is not the contention these cases are about.
    """
    return max(2, min(requested, os.cpu_count() or requested))


def _same_operation_worker(
    api: str,
    target: str,
    operation_id: str,
    content: bytes,
    vault: str,
    state: str,
) -> str:
    os.environ["LLM_WIKI_ROOT"] = vault
    os.environ["LLM_WIKI_STATE_ROOT"] = state
    import markdown_transaction

    path = Path(target)
    if api == "append":
        return _under_contention(
            markdown_transaction.append_knowledge, operation_id, path, content
        ).state
    return _under_contention(
        markdown_transaction.mutate_knowledge, operation_id, {path: content}
    ).state


def _distinct_append_worker(target: str, index: int, vault: str, state: str) -> str:
    os.environ["LLM_WIKI_ROOT"] = vault
    os.environ["LLM_WIKI_STATE_ROOT"] = state
    import markdown_transaction

    return _under_contention(
        markdown_transaction.append_knowledge,
        f"stress:{index}",
        Path(target),
        f"event-{index}\n".encode(),
    ).state


def _mixed_append_futures(executor, target: Path, vault: Path, state: Path):
    futures = []
    for index in range(12):
        futures.append(
            executor.submit(
                _distinct_append_worker,
                str(target),
                index,
                str(vault),
                str(state),
            )
        )
    for _index in range(6):
        futures.append(
            executor.submit(
                _same_operation_worker,
                "append",
                str(target),
                "mixed-stress:same",
                b"same\n",
                str(vault),
                str(state),
            )
        )
    return futures


def test_repository_scanner_finds_no_unapproved_covered_writers():
    from check_knowledge_writers import discover_repository_writers

    findings = discover_repository_writers(ROOT)
    assert findings, "scanner must discover the coordinator's target apply"
    offenders = [finding for finding in findings if not finding.approved]
    assert offenders == [], "\n" + "\n".join(str(item) for item in offenders)


def test_scanner_writer_set_equals_behavioral_matrix():
    from check_knowledge_writers import discover_repository_entrypoints

    assert (
        discover_repository_entrypoints(
            ROOT,
            files={
                "access_tracking.py",
                "archive_stale.py",
                "blackboard.py",
                "bootstrap_project.py",
                "build_guardrails.py",
                "build_context.py",
                "daily_log_append.py",
                "feedback_capture.py",
                "flush_memory.py",
                "migrate_to_okf.py",
                "query_memory.py",
                "rebuild_memory_index.py",
                "reflection.py",
                "session_end_project_tag.py",
                "session_start_project_state.py",
                "tool_breadcrumb_append.py",
                "user_prompt_capture.py",
            },
        )
        == TASK14_BEHAVIORAL_ENTRYPOINTS
    )


class _Drive:
    """Everything one entrypoint needs to be driven at its own boundary."""

    def __init__(self, entrypoint, module, tmp_path, monkeypatch, boundary, secret):
        self.entrypoint = entrypoint
        self.module = module
        self.function_name = entrypoint.split(":", 1)[1]
        self.function = getattr(module, self.function_name)
        self.tmp_path = tmp_path
        self.vault = tmp_path / "vault"
        self.monkeypatch = monkeypatch
        self.boundary = boundary
        self.secret = secret


def _drive_access_tracking(d: _Drive) -> None:
    import retrieval_telemetry

    module, monkeypatch, vault = d.module, d.monkeypatch, d.vault
    page = vault / "knowledge/notes/page.md"
    page.write_text("---\ntype: concept\n---\n# Page\n", encoding="utf-8")
    database = d.tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setattr(module, "KNOWLEDGE_DIR", page.parent)
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
    retrieval_telemetry.record_event(
        retrieval_telemetry.make_event(
            event_kind="page_read",
            query=None,
            retrieval_mode="direct",
            candidate_id="page",
            rank=None,
            generation="legacy",
            source_tool="writer-test",
        ),
        db_path=database,
    )
    d.function("page")


def _drive_archive_stale(d: _Drive) -> None:
    module, monkeypatch, vault = d.module, d.monkeypatch, d.vault
    page = vault / "knowledge/notes/page.md"
    page.write_text("---\ntype: debugging\n---\n# Page\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "KNOWLEDGE", page.parent)
    monkeypatch.setattr(module, "ARCHIVE_ROOT", page.parent / "archive")
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    d.function(page, True)


def _drive_blackboard(d: _Drive) -> None:
    d.monkeypatch.setattr(d.module, "append_knowledge", d.boundary)
    d.function(
        d.vault / "knowledge/projects/demo/.blackboard/signals.jsonl",
        {"message": d.secret},
    )


def _drive_bootstrap_project(d: _Drive) -> None:
    module, monkeypatch, vault, secret = d.module, d.monkeypatch, d.vault, d.secret
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "PROJECTS_DIR", vault / "knowledge/projects")
    monkeypatch.setattr(module, "_compute_slug", lambda cwd: "demo")
    monkeypatch.setattr(module, "_extract_git_timeline", lambda cwd: [])
    monkeypatch.setattr(module, "_extract_readme_summary", lambda cwd: secret)
    monkeypatch.setattr(module, "_extract_tech_stack", lambda cwd: [])
    monkeypatch.setattr(module, "_extract_docs_structure", lambda cwd: [])
    monkeypatch.setattr(module, "_run_git", lambda *args: "")
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    d.function(str(d.tmp_path), apply=True)


def _drive_build_guardrails(d: _Drive) -> None:
    module, monkeypatch, vault = d.module, d.monkeypatch, d.vault
    page = vault / "knowledge/notes/page.md"
    page.write_text(
        "---\ntype: pattern\n---\n# Page\n\nOne-sentence summary: Always use safe storage\n",
        encoding="utf-8",
    )
    target = vault / "knowledge/guardrails.md"
    target.write_bytes(b"old\n")
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "KNOWLEDGE", page.parent)
    monkeypatch.setattr(module, "FEEDBACK_DIR", vault / "knowledge/feedback")
    monkeypatch.setattr(module, "GUARDRAILS_FILE", target)
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary, raising=False)
    monkeypatch.setattr(sys, "argv", ["build_guardrails.py", "--apply"])
    d.function()


def _drive_build_context(d: _Drive) -> None:
    module, monkeypatch, vault, secret = d.module, d.monkeypatch, d.vault, d.secret
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "PROJECTS_DIR", vault / "knowledge/projects")
    monkeypatch.setattr(module, "build_context", lambda *args: secret)
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    monkeypatch.setattr(sys, "argv", ["build_context.py", "demo", "--write"])
    d.function()


def _drive_daily_log_append(d: _Drive) -> None:
    d.monkeypatch.setattr(d.module, "append_knowledge", d.boundary)
    path = d.vault / "knowledge/daily/2026-07-15.md"
    if d.function_name == "locked_append_once":
        d.function(path, d.secret, "event-1")
        return
    d.function(path, d.secret)


def _promoted_candidate(d: _Drive) -> None:
    candidate = d.vault / "knowledge/feedback/abcdef123456.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text(
        json.dumps(
            {
                "id": "abcdef123456",
                "type": "correction",
                "confidence": 0.7,
                "text": d.secret,
                "session_id": d.secret,
                "project": d.secret,
                "trigger": d.secret,
                "captured_at": "2026-01-01",
                "status": "candidate",
            }
        ),
        encoding="utf-8",
    )
    d.function("abcdef123456")


def _drive_feedback_capture(d: _Drive) -> None:
    module, monkeypatch, vault, secret = d.module, d.monkeypatch, d.vault, d.secret
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "FEEDBACK_DIR", vault / "knowledge/feedback")
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    if d.function_name != "capture_from_text":
        _promoted_candidate(d)
        return
    d.function("No, use safe storage", session_id=secret, slug=secret, trigger=secret)


def _drive_flush_memory(d: _Drive) -> None:
    d.monkeypatch.setattr(d.module, "DAILY_DIR", d.vault / "knowledge/daily")
    d.monkeypatch.setattr("daily_log_append.locked_append", d.boundary)
    d.function("2026-07-15", "safe", operation_id="event-1")


def _drive_migrate_to_okf(d: _Drive) -> None:
    page = d.vault / "knowledge/notes/page.md"
    page.write_text("# Page\n", encoding="utf-8")
    d.monkeypatch.setattr(d.module, "ROOT", d.vault)
    d.monkeypatch.setattr(d.module, "mutate_knowledge", d.boundary)
    d.function(page, "safe", "0" * 64)


def _drive_query_memory(d: _Drive) -> None:
    module, monkeypatch, vault, secret = d.module, d.monkeypatch, d.vault, d.secret
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "QA_DIR", vault / "knowledge/notes")
    monkeypatch.setattr(module, "LOG", vault / "knowledge/log.md")
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    monkeypatch.setattr("markdown_transaction.append_knowledge", d.boundary)
    if d.function_name == "file_back":
        d.function(secret, secret)
        return
    d.function(secret)


def _drive_rebuild_memory_index(d: _Drive) -> None:
    module, monkeypatch, vault = d.module, d.monkeypatch, d.vault
    monkeypatch.setattr(module, "out", vault / "knowledge/index.md")
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "build_index_bytes", lambda root, **kwargs: b"safe")
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    d.function()


def _drive_reflection(d: _Drive) -> None:
    module, monkeypatch, vault, secret = d.module, d.monkeypatch, d.vault, d.secret
    page = vault / "knowledge/notes/page.md"
    page.write_text(
        "---\ntype: concept\n---\n# Page\n\n## Update (2026-01-01)\na\n## Update (2026-01-02)\nb\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr("llm_client.call_llm", lambda *args, **kwargs: f"# Page\n\n{secret}")
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    d.function(page, apply=True)


def _drive_session_end_project_tag(d: _Drive) -> None:
    d.monkeypatch.setattr("daily_log_append.append_knowledge", d.boundary)
    d.function(d.vault / "knowledge/daily/2026-07-15.md", d.secret, "event-1")


def _drive_session_start_project_state(d: _Drive) -> None:
    module, monkeypatch, vault = d.module, d.monkeypatch, d.vault
    project = d.tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge/projects/_template/state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root: `<absolute-path>`\n", encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(module, "ProjectStore", lambda *args: object())
    monkeypatch.setattr(
        module,
        "recover_project_handoff",
        lambda *args, **kwargs: SimpleNamespace(context="", degraded=False, legacy=False),
    )
    monkeypatch.setattr(module, "mutate_knowledge", d.boundary)
    d.function()


def _drive_tool_breadcrumb_append(d: _Drive) -> None:
    d.monkeypatch.setattr("daily_log_append.append_daily", d.boundary)
    d.function(
        {
            "slug": "demo",
            "sessionId": "s1",
            "tool": "bash",
            "target": d.secret,
        }
    )


def _drive_user_prompt_capture(d: _Drive) -> None:
    d.monkeypatch.setattr("daily_log_append.append_daily", d.boundary)
    d.function("demo", "s1", d.secret, "event-1")


# One driver per writer module: the dispatch used to be a 24-branch chain.
_WRITER_DRIVERS = {
    "access_tracking": _drive_access_tracking,
    "archive_stale": _drive_archive_stale,
    "blackboard": _drive_blackboard,
    "bootstrap_project": _drive_bootstrap_project,
    "build_guardrails": _drive_build_guardrails,
    "build_context": _drive_build_context,
    "daily_log_append": _drive_daily_log_append,
    "feedback_capture": _drive_feedback_capture,
    "flush_memory": _drive_flush_memory,
    "migrate_to_okf": _drive_migrate_to_okf,
    "query_memory": _drive_query_memory,
    "rebuild_memory_index": _drive_rebuild_memory_index,
    "reflection": _drive_reflection,
    "session_end_project_tag": _drive_session_end_project_tag,
    "session_start_project_state": _drive_session_start_project_state,
    "tool_breadcrumb_append": _drive_tool_breadcrumb_append,
    "user_prompt_capture": _drive_user_prompt_capture,
}

# These writers legitimately hand the boundary content read from disk, so the
# secret they were seeded with is expected to appear in the recorded call.
_SECRET_BEARING_WRITERS = frozenset({
    "archive_stale",
    "flush_memory",
    "migrate_to_okf",
    "rebuild_memory_index",
})


def _executable_of(command):
    if isinstance(command, (list, tuple)) and command:
        return command[0]
    return command


def _assert_no_git(command, *args, **kwargs):
    assert str(_executable_of(command)).casefold() not in {"git", "git.exe"}
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _assert_boundary_use(entrypoint: str, module_name: str, calls: list, secret: str) -> None:
    assert calls, f"{entrypoint} did not delegate to the mutation boundary"
    if entrypoint in TASK14_READ_TRANSFORM_WRITE_ENTRYPOINTS:
        assert all(call_kwargs.get("preconditions") for _, call_kwargs in calls), (
            f"{entrypoint} did not bind its source snapshot"
        )
    if module_name not in _SECRET_BEARING_WRITERS:
        assert secret not in repr(calls), f"{entrypoint} leaked a secret before prepare"


@pytest.mark.parametrize("entrypoint", sorted(TASK14_BEHAVIORAL_ENTRYPOINTS))
def test_task14_actual_entrypoint_delegates_without_git(entrypoint, tmp_path, monkeypatch):
    module_name = Path(entrypoint.split(":", 1)[0]).stem
    module = importlib.import_module(module_name)
    vault, _ = _vault(tmp_path)
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    calls = []

    def boundary(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(id="tx", state="committed", preconditions={})

    monkeypatch.setattr(subprocess, "run", _assert_no_git)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))

    driver = _WRITER_DRIVERS.get(module_name)
    assert driver is not None, entrypoint
    driver(_Drive(entrypoint, module, tmp_path, monkeypatch, boundary, secret))

    _assert_boundary_use(entrypoint, module_name, calls, secret)


def test_scanner_keeps_runtime_writes_outside_boundary():
    from check_knowledge_writers import scan_source

    source = 'Path("cache/access.jsonl").write_text("x")\n'
    assert scan_source(Path("scripts/example.py"), source) == []


@pytest.mark.parametrize(
    ("source", "api"),
    [
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.open("w")\n', "open"),
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.write_text("x")\n', "write_text"),
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.touch()\n', "touch"),
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.unlink()\n', "unlink"),
        ('p = ROOT / "knowledge" / "notes"\np.mkdir()\n', "mkdir"),
        ('dst = ROOT / "knowledge" / "notes" / "x.md"\nos.replace("tmp", dst)\n', "replace"),
        ('dst = ROOT / "knowledge" / "notes" / "x.md"\nos.rename("tmp", dst)\n', "rename"),
        ('dst = ROOT / "knowledge" / "notes" / "x.md"\nshutil.move("tmp", dst)\n', "move"),
    ],
)
def test_python_scanner_resolves_write_destinations(source, api):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [(api, False)]


def test_python_scanner_recognizes_feedback_and_boundary_calls():
    from check_knowledge_writers import scan_source

    source = (
        "from markdown_transaction import mutate_knowledge\n"
        'p = ROOT / "knowledge" / "feedback" / "abcdef123456.json"\n'
        'mutate_knowledge("event", {p: b"x"})\n'
    )
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("mutate_knowledge", True)]


def test_python_scanner_detects_direct_guardrails_atomic_write():
    from check_knowledge_writers import scan_source

    source = (
        "from memory_state import atomic_write\n"
        'target = ROOT / "knowledge" / "guardrails.md"\n'
        'atomic_write(target, "unsafe")\n'
    )

    findings = scan_source(Path("scripts/probe.py"), source)

    assert [(item.api, item.approved) for item in findings] == [("atomic_write", False)]


def test_python_scanner_recognizes_checked_markdown_publication_boundary():
    from check_knowledge_writers import scan_source

    source = """
def _apply_windows_operation(staged, destination):
    durable_publish_file(
        staged,
        destination,
        replace=True,
        expected_sha256="0" * 64,
        max_bytes=1024,
    )
"""

    findings = scan_source(Path("scripts/markdown_transaction.py"), source)

    assert [(item.api, item.approved) for item in findings] == [("durable_publish_file", True)]


def test_python_scanner_traces_parameter_sink_alias_and_path_alias():
    from check_knowledge_writers import scan_source

    source = """
from pathlib import Path as P
ROOT_DIR = P("knowledge")
def raw_write(destination):
    destination.write_text("x")
writer = raw_write
target = ROOT_DIR / "notes" / "x.md"
writer(target)
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("raw_write", False, "<module>")
    ]


def test_python_scanner_traces_method_path_parameter_to_callsite():
    from check_knowledge_writers import scan_source

    source = """
from pathlib import Path
class Writer:
    def save(self, destination):
        destination.write_bytes(b"x")
target = Path("knowledge/notes/x.md")
Writer().save(target)
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("save", False, "<module>")
    ]


@pytest.mark.parametrize(
    ("source", "approved"),
    [
        (
            "from scripts.markdown_transaction import mutate_knowledge as mutate\n"
            'p = Path("knowledge/notes/x.md")\nmutate("id", {p: b"x"})\n',
            True,
        ),
        (
            "def mutate_knowledge(operation_id, changes):\n    return None\n"
            'p = Path("knowledge/notes/x.md")\nmutate_knowledge("id", {p: b"x"})\n',
            False,
        ),
    ],
)
def test_python_scanner_proves_canonical_boundary_binding(source, approved):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("mutate_knowledge" if not approved else "mutate", approved)
    ]


def test_python_scanner_tracks_canonical_module_and_function_aliases():
    from check_knowledge_writers import scan_source

    source = """
import scripts.markdown_transaction as transactions
from pathlib import Path as P
safe_mutate = transactions.mutate_knowledge
target = P("knowledge") / "notes" / "x.md"
safe_mutate("id", {target: b"x"})
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("safe_mutate", True)]


@pytest.mark.parametrize(
    "source",
    [
        """
p = Path("cache/x")
if enabled:
    p = Path("knowledge/notes/x.md")
else:
    p = Path("cache/y")
p.write_text("x")
""",
        """
p = Path("cache/x")
for candidate in candidates:
    if candidate:
        p = Path("knowledge/notes/x.md")
p.write_text("x")
""",
        """
p = Path("cache/x")
while pending:
    p = Path("knowledge/notes/x.md")
p.write_text("x")
""",
        """
p = Path("cache/x")
try:
    p = Path("knowledge/notes/x.md")
except OSError:
    p = Path("cache/y")
finally:
    p.write_text("x")
""",
        """
p = Path("cache/x")
try:
    p = Path("knowledge/notes/x.md")
    risky()
    p = Path("cache/y")
except OSError:
    pass
finally:
    p.write_text("x")
""",
        """
p = Path("cache/x")
match kind:
    case "note":
        p = Path("knowledge/notes/x.md")
    case _:
        p = Path("cache/y")
writer = lambda: p.write_text("x")
[writer() for _ in range(1)]
""",
    ],
)
def test_python_scanner_preserves_covered_paths_across_control_flow(source):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("write_text", False)]


def test_python_scanner_analyzes_nested_helper_definitions():
    from check_knowledge_writers import scan_source

    source = """
def outer(destination):
    def helper(path):
        with path.open("w") as stream:
            stream.write("x")
    helper(destination)
outer(Path("knowledge/notes/x.md"))
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("outer", False, "<module>")
    ]


def test_python_scanner_merges_function_aliases_across_branches():
    from check_knowledge_writers import scan_source

    source = """
def raw(path):
    path.write_text("x")
def harmless(path):
    return path
if enabled:
    writer = raw
else:
    writer = harmless
writer(Path("knowledge/notes/x.md"))
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("writer", False)]


def test_python_scanner_drops_path_taint_after_exhaustive_safe_reassignment():
    from check_knowledge_writers import scan_source

    source = """
p = Path("knowledge/notes/x.md")
if enabled:
    p = Path("cache/x")
else:
    p = Path("logs/x")
p.write_text("x")
"""
    assert scan_source(Path("scripts/probe.py"), source) == []


def test_python_scanner_drops_alias_taint_after_exhaustive_safe_reassignment():
    from check_knowledge_writers import scan_source

    source = """
def raw(path):
    path.write_text("x")
def harmless(path):
    return path
writer = raw
if enabled:
    writer = harmless
else:
    writer = harmless
writer(Path("knowledge/notes/x.md"))
"""
    assert scan_source(Path("scripts/probe.py"), source) == []


def test_python_scanner_analyzes_definition_time_expressions():
    from check_knowledge_writers import scan_source

    source = """
def sink(path):
    path.write_text("x")
target = Path("knowledge/notes/x.md")
@sink(target)
def decorated(
    annotated: sink(target),
    defaulted=sink(target),
    *, keyword=sink(target),
) -> sink(target):
    pass
@sink(target)
class Defined(
    sink(target),
    metaclass=sink(target),
):
    pass
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("sink", False),
    ] * 8


@pytest.mark.parametrize(
    ("suffix", "source", "expected_api"),
    [
        (
            ".js",
            'const p = root + "/knowledge/notes/x.md";\nfs.writeFileSync(p, data);\n',
            "writeFileSync",
        ),
        (
            ".ps1",
            '$p = Join-Path $root "knowledge/notes/x.md"\nSet-Content -Path $p -Value $x\n',
            "Set-Content",
        ),
        (".sh", 'p="$root/knowledge/notes/x.md"\nprintf "%s" "$x" > "$p"\n', ">"),
    ],
)
def test_non_python_scanner_resolves_variable_write_destinations(suffix, source, expected_api):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path(f"scripts/probe{suffix}"), source)
    assert [(item.api, item.approved) for item in findings] == [(expected_api, False)]


@pytest.mark.parametrize(
    ("suffix", "source"),
    [
        (
            ".js",
            '// fs.writeFileSync("knowledge/notes/x.md", x)\nconst s = "writeFile knowledge/notes/x.md";\n',
        ),
        (".ps1", '# Set-Content knowledge/notes/x.md\n$s = "Set-Content knowledge/notes/x.md"\n'),
        (".sh", '# echo x > knowledge/notes/x.md\ns="echo x > knowledge/notes/x.md"\n'),
    ],
)
def test_non_python_scanner_ignores_comments_and_strings(suffix, source):
    from check_knowledge_writers import scan_source

    assert scan_source(Path(f"scripts/probe{suffix}"), source) == []


@pytest.mark.parametrize(
    ("suffix", "source", "api"),
    [
        (
            ".js",
            'const target = root +\n  "/knowledge/notes/x.md";\n'
            "fs.writeFileSync(\n  target,\n  data\n);\n",
            "writeFileSync",
        ),
        (
            ".ps1",
            '$target = Join-Path `\n  $root `\n  "knowledge/notes/x.md"\n'
            "Set-Content `\n  -Path $target `\n  -Value $data\n",
            "Set-Content",
        ),
        (
            ".sh",
            'target="$root/knowledge/notes/x.md"\nprintf "%s" \\\n  "$data" \\\n  > "$target"\n',
            ">",
        ),
    ],
)
def test_non_python_scanner_parses_multiline_constructs(suffix, source, api):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path(f"scripts/probe{suffix}"), source)
    assert [(item.api, item.approved) for item in findings] == [(api, False)]


def test_append_knowledge_is_idempotent_and_records_expected_hash(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "daily" / "2026-07-15.md"

    first = markdown_transaction.append_knowledge("daily:event-1", path, b"one\n")
    second = markdown_transaction.append_knowledge("daily:event-1", path, b"one\n")

    assert first.id == second.id
    assert path.read_bytes() == b"one\n"
    assert first.preconditions["knowledge/daily/2026-07-15.md"] == "absent"


def test_stable_append_retries_against_an_existing_empty_file(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "daily" / "2026-07-15.md"
    path.write_bytes(b"")
    first = markdown_transaction.append_knowledge("empty:event", path, b"one\n")
    second = markdown_transaction.append_knowledge("empty:event", path, b"one\n")
    assert second.id == first.id
    assert path.read_bytes() == b"one\n"


def test_generic_append_without_occurrence_keeps_identical_events(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "daily" / "2026-07-15.md"

    markdown_transaction.append_knowledge(None, path, b"same\n")
    markdown_transaction.append_knowledge(None, path, b"same\n")
    assert path.read_bytes() == b"same\nsame\n"


def test_locked_append_and_blackboard_use_fresh_occurrences_by_default(tmp_path, monkeypatch):
    import blackboard
    import daily_log_append

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setattr(blackboard, "PROJECTS_DIR", vault / "knowledge" / "projects")
    daily = vault / "knowledge" / "daily" / "2026-07-15.md"
    daily_log_append.locked_append(daily, "same")
    daily_log_append.locked_append(daily, "same")
    events = vault / "knowledge" / "projects" / "demo" / ".blackboard" / "signals.jsonl"
    blackboard._append_jsonl(events, {"message": "same"})
    blackboard._append_jsonl(events, {"message": "same"})
    assert daily.read_text(encoding="utf-8").count("same") == 2
    assert events.read_text(encoding="utf-8").count('"message": "same"') == 2


def test_concurrent_daily_and_project_jsonl_appends_never_interleave(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    daily = vault / "knowledge" / "daily" / "2026-07-15.md"
    jsonl = vault / "knowledge" / "projects" / "demo" / ".blackboard" / "tasks.jsonl"

    def append(index: int) -> None:
        _under_contention(
            markdown_transaction.append_knowledge,
            f"daily:event-{index}",
            daily,
            f"D{index:02d}-start\nD{index:02d}-end\n".encode(),
        )
        _under_contention(
            markdown_transaction.append_knowledge,
            f"project:event-{index}",
            jsonl,
            f'{{"id":{index}}}\n'.encode(),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=_bounded_workers(8)) as executor:
        list(executor.map(append, range(12)))

    daily_text = daily.read_text(encoding="utf-8")
    jsonl_text = jsonl.read_text(encoding="utf-8")
    for index in range(12):
        assert daily_text.count(f"D{index:02d}-start\nD{index:02d}-end\n") == 1
        assert jsonl_text.count(f'{{"id":{index}}}\n') == 1


@pytest.mark.parametrize("api", ["append", "mutation"])
@pytest.mark.parametrize("executor_type", ["thread", "process"])
def test_concurrent_identical_operation_id_converges_once(
    tmp_path, monkeypatch, api, executor_type
):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    target = vault / "knowledge" / "notes" / "same.md"
    operation_id = f"same-{api}"
    content = b"same\n"
    workers = 10
    executor_class = (
        concurrent.futures.ThreadPoolExecutor
        if executor_type == "thread"
        else concurrent.futures.ProcessPoolExecutor
    )
    with executor_class(max_workers=_bounded_workers(workers)) as executor:
        futures = [
            executor.submit(
                _same_operation_worker,
                api,
                str(target),
                operation_id,
                content,
                str(vault),
                str(state),
            )
            for _ in range(workers)
        ]
        assert [future.result(timeout=300) for future in futures] == ["committed"] * workers

    assert target.read_bytes() == content
    coordinator = markdown_transaction.MarkdownCoordinator(vault, state)
    record = coordinator._record_for_operation_id(operation_id)
    assert record is not None
    assert record.state == "committed"
    with coordinator._connect() as database:
        transaction_count = database.execute(
            'SELECT COUNT(*) FROM "transaction" WHERE operation_id = ? OR operation_id LIKE ?',
            (operation_id, f"{operation_id}:cas:%"),
        ).fetchone()[0]
    assert transaction_count == 1


def test_concurrent_identical_append_converges_once_during_distinct_event_churn(
    tmp_path, monkeypatch
):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    target = vault / "knowledge" / "daily" / "mixed-stress.md"
    operation_id = "mixed-stress:same"

    with concurrent.futures.ProcessPoolExecutor(max_workers=_bounded_workers(8)) as executor:
        futures = _mixed_append_futures(executor, target, vault, state)
        assert [future.result(timeout=300) for future in futures] == ["committed"] * 18

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines.count("same") == 1
    lines.remove("same")
    assert sorted(lines) == [
        "event-0",
        "event-1",
        "event-10",
        "event-11",
        "event-2",
        "event-3",
        "event-4",
        "event-5",
        "event-6",
        "event-7",
        "event-8",
        "event-9",
    ]
    coordinator = markdown_transaction.MarkdownCoordinator(vault, state)
    with coordinator._connect() as database:
        committed = database.execute(
            "SELECT COUNT(*) FROM \"transaction\" WHERE state = 'committed' "
            "AND (operation_id = ? OR operation_id LIKE ?)",
            (operation_id, f"{operation_id}:cas:%"),
        ).fetchone()[0]
    assert committed == 1


@pytest.mark.parametrize("executor_type", ["thread", "process"])
def test_distinct_events_survive_repeated_writer_contention(tmp_path, monkeypatch, executor_type):
    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    target = vault / "knowledge" / "daily" / "stress.md"
    executor_class = (
        concurrent.futures.ThreadPoolExecutor
        if executor_type == "thread"
        else concurrent.futures.ProcessPoolExecutor
    )

    with executor_class(max_workers=_bounded_workers(6)) as executor:
        futures = [
            executor.submit(
                _distinct_append_worker,
                str(target),
                index,
                str(vault),
                str(state),
            )
            for index in range(18)
        ]
        assert [future.result(timeout=300) for future in futures] == ["committed"] * 18

    content = target.read_text(encoding="utf-8")
    assert sorted(content.splitlines()) == sorted(f"event-{index}" for index in range(18))


@pytest.mark.parametrize("api", ["append", "mutate"])
@pytest.mark.parametrize(
    "contention",
    [
        sqlite3.OperationalError("database is busy"),
        sqlite3.OperationalError("database is locked"),
        PermissionError(32, "sharing violation"),
    ],
)
def test_public_mutation_retries_initial_recovery_contention_without_dropping_event(
    tmp_path, monkeypatch, api, contention
):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    # Short enough to keep the retry quick, long enough that the mutation
    # itself fits inside the derived recovery deadline on a slow runner.
    monkeypatch.setattr(markdown_transaction, "_WRITER_WAIT_SECONDS", 15.0)
    target = vault / "knowledge" / "notes" / f"{api}.md"
    real_recover = markdown_transaction.MarkdownCoordinator.recover
    attempts = 0

    def contended_recover(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise contention
        return real_recover(self, *args, **kwargs)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "recover", contended_recover)
    if api == "append":
        record = markdown_transaction.append_knowledge("recover:append", target, b"event\n")
    else:
        record = markdown_transaction.mutate_knowledge("recover:mutate", {target: b"event\n"})

    assert attempts >= 3
    assert record.state == "committed"
    assert target.read_bytes() == b"event\n"


def _conflict_feedback(vault, monkeypatch):
    import feedback_capture as module

    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "FEEDBACK_DIR", vault / "knowledge/feedback")
    module.FEEDBACK_DIR.mkdir()
    source = module.FEEDBACK_DIR / "abcdef123456.json"
    source.write_text(
        json.dumps(
            {
                "id": "abcdef123456",
                "type": "correction",
                "confidence": 0.8,
                "text": "Use safe storage",
                "session_id": "s1",
                "project": "demo",
                "trigger": "test",
                "captured_at": "2026-01-01",
                "status": "candidate",
            }
        ),
        encoding="utf-8",
    )
    destination = vault / "knowledge/notes/feedback-abcdef12.md"

    def invoke():
        return module.promote_candidate("abcdef123456")

    return module, source, destination, invoke


def _conflict_migration(vault, monkeypatch):
    import migrate_to_okf as module

    monkeypatch.setattr(module, "ROOT", vault)
    source = vault / "knowledge/notes/page.md"
    source.write_text("# Original\n", encoding="utf-8")
    monkeypatch.setattr(
        module, "parse_args", lambda: SimpleNamespace(scope="wiki", apply=True, report=False)
    )
    monkeypatch.setattr(module, "collect_files", lambda scope: [source])
    return module, source, None, module.main


def _conflict_reflection(vault, monkeypatch):
    import reflection as module

    monkeypatch.setattr(module, "ROOT", vault)
    source = vault / "knowledge/notes/page.md"
    source.write_text(
        "---\ntype: concept\n---\n# Original\n\n"
        "## Update (2026-01-01)\na\n## Update (2026-01-02)\nb\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("llm_client.call_llm", lambda *args, **kwargs: "# Reflected\n\nMerged")

    def invoke():
        return module.reflect_page(source, apply=True)

    return module, source, None, invoke


_CONFLICT_SETUPS = {
    "feedback": _conflict_feedback,
    "migration": _conflict_migration,
    "reflection": _conflict_reflection,
}


def _assert_destination_absent(destination) -> None:
    if destination is not None:
        assert not destination.exists()


def _refused(result) -> bool:
    return result == 1 or "error" in str(result).casefold()


@pytest.mark.parametrize("entrypoint", sorted(_CONFLICT_SETUPS))
def test_read_transform_write_conflict_preserves_user_bytes(tmp_path, monkeypatch, entrypoint):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    original_mutate = markdown_transaction.mutate_knowledge
    user_bytes = b"# Concurrent user edit\n"
    module, source, destination, invoke = _CONFLICT_SETUPS[entrypoint](vault, monkeypatch)

    def race(operation_id, changes, **kwargs):
        source.write_bytes(user_bytes)
        return original_mutate(operation_id, changes, **kwargs)

    monkeypatch.setattr(module, "mutate_knowledge", race)
    try:
        result = invoke()
    except ValueError:
        result = 1

    assert _refused(result)
    assert source.read_bytes() == user_bytes
    _assert_destination_absent(destination)


@pytest.mark.parametrize("race", ["add", "delete", "change"])
def test_rebuild_index_retries_tree_race_with_fresh_snapshot(tmp_path, monkeypatch, race):
    import markdown_transaction
    import rebuild_memory_index

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    notes = vault / "knowledge" / "notes"
    primary = notes / "primary.md"
    primary.write_text(
        "---\ntype: concept\n---\n# Primary\n\nOne-sentence summary: old summary\n",
        encoding="utf-8",
    )
    victim = notes / "victim.md"
    if race == "delete":
        victim.write_text(
            "---\ntype: concept\n---\n# Victim\n\nOne-sentence summary: delete me\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(rebuild_memory_index, "ROOT", vault)
    monkeypatch.setattr(rebuild_memory_index, "out", vault / "knowledge/index.md")
    original_mutate = markdown_transaction.mutate_knowledge
    injected = False

    def racing_mutate(operation_id, changes, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            if race == "add":
                (notes / "added.md").write_text(
                    "---\ntype: concept\n---\n# Added\n\nOne-sentence summary: added summary\n",
                    encoding="utf-8",
                )
            elif race == "delete":
                victim.unlink()
            else:
                primary.write_text(
                    "---\ntype: concept\n---\n# Primary\n\nOne-sentence summary: fresh summary\n",
                    encoding="utf-8",
                )
        return original_mutate(operation_id, changes, **kwargs)

    monkeypatch.setattr(rebuild_memory_index, "mutate_knowledge", racing_mutate)
    assert rebuild_memory_index.main() == 0

    index = rebuild_memory_index.out.read_text(encoding="utf-8")
    if race == "add":
        assert "[[knowledge/notes/added]]" in index
        assert "added summary" in index
    elif race == "delete":
        assert "victim" not in index
        assert "delete me" not in index
    else:
        assert "fresh summary" in index
        assert "old summary" not in index


@pytest.mark.parametrize("drift", ["delete", "modify"])
def test_rebuild_index_repairs_committed_index_drift_with_new_transaction(
    tmp_path, monkeypatch, drift
):
    import markdown_transaction
    import rebuild_memory_index

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    page = vault / "knowledge/notes/page.md"
    page.write_text(
        "---\ntype: concept\n---\n# Page\n\nOne-sentence summary: durable summary\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rebuild_memory_index, "ROOT", vault)
    monkeypatch.setattr(rebuild_memory_index, "out", vault / "knowledge/index.md")
    assert rebuild_memory_index.main() == 0
    expected = rebuild_memory_index.out.read_bytes()
    if drift == "delete":
        rebuild_memory_index.out.unlink()
    else:
        rebuild_memory_index.out.write_bytes(b"corrupt index\n")

    assert rebuild_memory_index.main() == 0
    assert rebuild_memory_index.out.read_bytes() == expected
    coordinator = markdown_transaction.MarkdownCoordinator(vault, state)
    with coordinator._connect() as database:
        rows = database.execute(
            'SELECT operation_id FROM "transaction" '
            'WHERE operation_id LIKE "rebuild-index:%" ORDER BY created_at'
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["operation_id"] != rows[1]["operation_id"]


def test_append_retries_a_cas_conflict_without_overwriting_winner(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "daily" / "2026-07-15.md"
    original_prepare = markdown_transaction.MarkdownCoordinator.prepare
    injected = False

    def racing_prepare(self, changes, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            markdown_transaction.append_knowledge("daily:winner", path, b"winner\n")
        return original_prepare(self, changes, **kwargs)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "prepare", racing_prepare)
    markdown_transaction.append_knowledge("daily:loser", path, b"loser\n")

    assert path.read_bytes() == b"winner\nloser\n"


@pytest.mark.parametrize(("relative", "content"), WRITER_TARGETS)
def test_mutation_recovers_after_crash_at_prepared_state(tmp_path, monkeypatch, relative, content):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    target = vault / relative
    original = markdown_transaction.MarkdownCoordinator._killpoint

    def crash(self, name, parent_transaction_id=None):
        if name == "after_prepared":
            raise KeyboardInterrupt("injected crash")
        return original(self, name, parent_transaction_id)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", crash)
    with pytest.raises(KeyboardInterrupt, match="injected crash"):
        markdown_transaction.mutate_knowledge(f"crash:{relative}", {target: content})

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", original)
    record = markdown_transaction.mutate_knowledge(f"crash:{relative}", {target: content})
    assert record.state == "committed"
    assert target.exists()


@pytest.mark.parametrize(("relative", "content"), WRITER_TARGETS)
def test_mutation_recovers_after_crash_during_apply(tmp_path, monkeypatch, relative, content):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    target = vault / relative
    original = markdown_transaction.MarkdownCoordinator._killpoint

    def crash(self, name, parent_transaction_id=None):
        if name == "after_each_target":
            raise KeyboardInterrupt("injected apply crash")
        return original(self, name, parent_transaction_id)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", crash)
    with pytest.raises(KeyboardInterrupt, match="injected apply crash"):
        markdown_transaction.mutate_knowledge(f"apply-crash:{relative}", {target: content})
    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", original)
    record = markdown_transaction.mutate_knowledge(f"apply-crash:{relative}", {target: content})
    assert record.state == "committed"
    assert target.read_bytes() == content


@pytest.mark.parametrize(("relative", "content"), WRITER_TARGETS)
def test_unknown_external_edit_conflicts_without_overwrite(
    tmp_path, monkeypatch, relative, content
):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"before")
    original_prepare = markdown_transaction.MarkdownCoordinator.prepare
    injected = False

    def external_edit(self, changes, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            target.write_bytes(b"external")
        return original_prepare(self, changes, **kwargs)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "prepare", external_edit)
    with pytest.raises(ValueError, match="precondition changed"):
        markdown_transaction.mutate_knowledge(f"external:{relative}", {target: content})
    assert target.read_bytes() == b"external"


def test_secure_parent_creation_rejects_symlink(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault / "knowledge" / "projects" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))

    with pytest.raises((ValueError, RuntimeError)):
        markdown_transaction.append_knowledge("project:escape", link / "events.jsonl", b"{}\n")
    assert list(outside.iterdir()) == []


def test_operation_ids_are_stable_content_keys():
    from markdown_transaction import stable_operation_id

    value = stable_operation_id("feedback", "session-1", b"redacted bytes")
    assert value == stable_operation_id("feedback", "session-1", b"redacted bytes")
    assert hashlib.sha256(b"redacted bytes").hexdigest() in value


def test_append_signatures_keep_optional_operation_identity():
    from daily_log_append import locked_append
    from markdown_transaction import append_knowledge

    assert inspect.signature(append_knowledge).parameters["operation_id"].default is None
    assert inspect.signature(locked_append).parameters["operation_id"].default is None


@pytest.mark.parametrize("initial, desired", [(None, b"created"), (b"old", b"new"), (b"old", None)])
def test_mutate_replays_identical_committed_create_replace_delete(
    tmp_path, monkeypatch, initial, desired
):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "notes" / "page.md"
    if initial is not None:
        path.write_bytes(initial)
    first = markdown_transaction.mutate_knowledge("replay:event", {path: desired})
    second = markdown_transaction.mutate_knowledge("replay:event", {path: desired})
    assert second.id == first.id
    assert path.read_bytes() == desired if desired is not None else not path.exists()


@pytest.mark.parametrize("drift", ["delete", "modify"])
def test_mutate_committed_replay_reports_target_drift(tmp_path, monkeypatch, drift):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge/notes/page.md"
    markdown_transaction.mutate_knowledge("replay:drift", {path: b"expected"})
    if drift == "delete":
        path.unlink()
    else:
        path.write_bytes(b"user-modified")

    with pytest.raises(RuntimeError, match=r"drift.*new operation_id"):
        markdown_transaction.mutate_knowledge("replay:drift", {path: b"expected"})

    if drift == "delete":
        assert not path.exists()
    else:
        assert path.read_bytes() == b"user-modified"


@pytest.mark.parametrize("api", ["append", "mutation"])
def test_writer_rejects_rebound_committed_operation(tmp_path, monkeypatch, api):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "notes" / "page.md"
    if api == "append":
        markdown_transaction.append_knowledge("replay:event", path, b"one")
    else:
        markdown_transaction.mutate_knowledge("replay:event", {path: b"one"})
    with pytest.raises(ValueError, match="different request"):
        if api == "append":
            markdown_transaction.append_knowledge("replay:event", path, b"two")
        else:
            markdown_transaction.mutate_knowledge("replay:event", {path: b"two"})


@pytest.mark.parametrize(
    "relative",
    [
        "knowledge/notes/data.json",
        "knowledge/feedback/not-hex.json",
        "knowledge/projects/demo/events.jsonl",
        "knowledge/projects/demo/.blackboard/other.jsonl",
        "knowledge/notes/" + "a" * 129 + ".md",
        "knowledge/notes/a/b/c/d/e/f/g/h/i/j/k/l/m.md",
    ],
)
def test_target_contract_rejects_wrong_type_or_unbounded_path(tmp_path, relative):
    from markdown_transaction import MarkdownChange, MarkdownCoordinator

    vault, state = _vault(tmp_path)
    target = vault / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    coordinator = MarkdownCoordinator(vault, state)
    with pytest.raises(ValueError):
        coordinator.prepare([MarkdownChange.create(relative, b"x")], operation_id=relative)


def test_append_rejects_oversize_block_and_prospective_target(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "daily" / "2026-07-15.md"
    monkeypatch.setattr(markdown_transaction, "MAX_KNOWLEDGE_TARGET_BYTES", 8)
    with pytest.raises(ValueError, match="size"):
        markdown_transaction.append_knowledge(None, path, b"123456789")
    path.write_bytes(b"123456")
    with pytest.raises(ValueError, match="size"):
        markdown_transaction.append_knowledge(None, path, b"789")


def test_feedback_redacts_all_metadata_before_prepare(tmp_path, monkeypatch):
    import feedback_capture

    vault, state = _vault(tmp_path)
    feedback = vault / "knowledge" / "feedback"
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setattr(feedback_capture, "ROOT", vault)
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", feedback)
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    candidate_id = feedback_capture.capture_from_text(
        "No, use safe storage instead",
        session_id=f"session-{secret}",
        slug=f"project-{secret}",
        trigger=f"trigger-{secret}",
    )
    raw = (feedback / f"{candidate_id}.json").read_text(encoding="utf-8")
    assert secret not in raw
    assert raw.count("[REDACTED") == 3
    assert json.loads(raw)["text"] == "No, use safe storage instead"


def test_archive_exception_is_function_and_operation_scoped():
    from check_knowledge_writers import scan_source

    source = """
def _build_bag_contents():
    hidden_build = archive_root / ".bag-building"
    (hidden_build / "bagit.txt").write_bytes(data)
def _publish_build():
    hidden_build.replace(final_bag)
def _bad_flat_write():
    flat = ROOT / "knowledge" / "daily" / "2026-01-01.md"
    flat.write_bytes(data)
"""
    findings = scan_source(Path("scripts/archive_daily.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("write_bytes", True),
        ("replace", True),
        ("write_bytes", False),
    ]
