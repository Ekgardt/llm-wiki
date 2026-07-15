from __future__ import annotations

import concurrent.futures
import hashlib
import importlib
import inspect
import io
import json
import subprocess
import sys
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
    ("knowledge/projects/demo/.blackboard/tasks.jsonl", b"{}\n"),
]

TASK14_BEHAVIORAL_ENTRYPOINTS = {
    "scripts/access_tracking.py:flush_access_to_frontmatter",
    "scripts/archive_stale.py:_archive_page",
    "scripts/blackboard.py:_append_jsonl",
    "scripts/bootstrap_project.py:bootstrap",
    "scripts/build_context.py:main",
    "scripts/daily_log_append.py:locked_append",
    "scripts/daily_log_append.py:locked_append_once",
    "scripts/feedback_capture.py:capture_from_text",
    "scripts/feedback_capture.py:promote_candidate",
    "scripts/flush_memory.py:append_daily",
    "scripts/migrate_to_okf.py:main",
    "scripts/query_memory.py:append_log",
    "scripts/query_memory.py:file_back",
    "scripts/rebuild_memory_index.py:main",
    "scripts/reflection.py:reflect_page",
    "scripts/session_end_project_tag.py:_append_entry",
    "scripts/session_start_project_state.py:main",
    "scripts/tool_breadcrumb_append.py:main",
    "scripts/user_prompt_capture.py:_append_prompt_tag",
}


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes").mkdir()
    (vault / "knowledge" / "projects").mkdir()
    (vault / "knowledge" / "inbox").mkdir()
    return vault, state


def test_repository_scanner_finds_no_unapproved_covered_writers():
    from check_knowledge_writers import discover_repository_writers

    findings = discover_repository_writers(ROOT)
    assert findings, "scanner must discover the coordinator's target apply"
    offenders = [finding for finding in findings if not finding.approved]
    assert offenders == [], "\n" + "\n".join(str(item) for item in offenders)


def test_scanner_writer_set_equals_behavioral_matrix():
    from check_knowledge_writers import discover_repository_entrypoints

    assert discover_repository_entrypoints(ROOT, files={
        "access_tracking.py", "archive_stale.py", "blackboard.py",
        "bootstrap_project.py", "build_context.py", "daily_log_append.py",
        "feedback_capture.py", "flush_memory.py", "migrate_to_okf.py",
        "query_memory.py", "rebuild_memory_index.py", "reflection.py",
        "session_end_project_tag.py", "session_start_project_state.py",
        "tool_breadcrumb_append.py", "user_prompt_capture.py",
    }) == TASK14_BEHAVIORAL_ENTRYPOINTS


@pytest.mark.parametrize("entrypoint", sorted(TASK14_BEHAVIORAL_ENTRYPOINTS))
def test_task14_actual_entrypoint_delegates_without_git(
    entrypoint, tmp_path, monkeypatch
):
    module_name = Path(entrypoint.split(":", 1)[0]).stem
    function_name = entrypoint.split(":", 1)[1]
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    vault, _ = _vault(tmp_path)
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    calls = []

    def boundary(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(id="tx", state="committed", preconditions={})

    def guarded_run(command, *args, **kwargs):
        executable = command[0] if isinstance(command, (list, tuple)) and command else command
        assert str(executable).casefold() not in {"git", "git.exe"}
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))

    if module_name == "access_tracking":
        page = vault / "knowledge/notes/page.md"
        page.write_text("---\ntype: concept\n---\n# Page\n", encoding="utf-8")
        monkeypatch.setattr(module, "KNOWLEDGE_DIR", page.parent)
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        module._batch.clear()
        module._batch["page"] = 1
        function("page")
    elif module_name == "archive_stale":
        page = vault / "knowledge/notes/page.md"
        page.write_text("---\ntype: debugging\n---\n# Page\n", encoding="utf-8")
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr(module, "KNOWLEDGE", page.parent)
        monkeypatch.setattr(module, "ARCHIVE_ROOT", page.parent / "archive")
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        function(page, True)
    elif module_name == "blackboard":
        monkeypatch.setattr(module, "append_knowledge", boundary)
        function(
            vault / "knowledge/projects/demo/.blackboard/signals.jsonl",
            {"message": secret},
        )
    elif module_name == "bootstrap_project":
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr(module, "PROJECTS_DIR", vault / "knowledge/projects")
        monkeypatch.setattr(module, "_compute_slug", lambda cwd: "demo")
        monkeypatch.setattr(module, "_extract_git_timeline", lambda cwd: [])
        monkeypatch.setattr(module, "_extract_readme_summary", lambda cwd: secret)
        monkeypatch.setattr(module, "_extract_tech_stack", lambda cwd: [])
        monkeypatch.setattr(module, "_extract_docs_structure", lambda cwd: [])
        monkeypatch.setattr(module, "_run_git", lambda *args: "")
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        function(str(tmp_path), apply=True)
    elif module_name == "build_context":
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr(module, "PROJECTS_DIR", vault / "knowledge/projects")
        monkeypatch.setattr(module, "build_context", lambda *args: secret)
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        monkeypatch.setattr(sys, "argv", ["build_context.py", "demo", "--write"])
        function()
    elif module_name == "daily_log_append":
        monkeypatch.setattr(module, "append_knowledge", boundary)
        path = vault / "knowledge/daily/2026-07-15.md"
        if function_name == "locked_append_once":
            function(path, secret, "event-1")
        else:
            function(path, secret)
    elif module_name == "feedback_capture":
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr(module, "FEEDBACK_DIR", vault / "knowledge/feedback")
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        if function_name == "capture_from_text":
            function("No, use safe storage", session_id=secret, slug=secret, trigger=secret)
        else:
            candidate = vault / "knowledge/feedback/abcdef123456.json"
            candidate.parent.mkdir(parents=True)
            candidate.write_text(json.dumps({
                "id": "abcdef123456", "type": "correction", "confidence": 0.7,
                "text": secret, "session_id": secret, "project": secret,
                "trigger": secret, "captured_at": "2026-01-01", "status": "candidate",
            }), encoding="utf-8")
            function("abcdef123456")
    elif module_name == "flush_memory":
        monkeypatch.setattr(module, "DAILY_DIR", vault / "knowledge/daily")
        monkeypatch.setattr("daily_log_append.locked_append", boundary)
        function("2026-07-15", "safe", operation_id="event-1")
    elif module_name == "migrate_to_okf":
        page = vault / "knowledge/notes/page.md"
        page.write_text("# Page\n", encoding="utf-8")
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr(module, "parse_args", lambda: SimpleNamespace(scope="wiki", apply=True, report=False))
        monkeypatch.setattr(module, "collect_files", lambda scope: [page])
        monkeypatch.setattr(module, "migrate_file", lambda path: ("migrate", "safe"))
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        function()
    elif module_name == "query_memory":
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr(module, "QA_DIR", vault / "knowledge/notes")
        monkeypatch.setattr(module, "LOG", vault / "knowledge/log.md")
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        monkeypatch.setattr("markdown_transaction.append_knowledge", boundary)
        if function_name == "file_back":
            function(secret, secret)
        else:
            function(secret)
    elif module_name == "rebuild_memory_index":
        monkeypatch.setattr(module, "out", vault / "knowledge/index.md")
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr(module, "build_index_bytes", lambda root: b"safe")
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        function()
    elif module_name == "reflection":
        page = vault / "knowledge/notes/page.md"
        page.write_text("---\ntype: concept\n---\n# Page\n\n## Update (2026-01-01)\na\n## Update (2026-01-02)\nb\n", encoding="utf-8")
        monkeypatch.setattr(module, "ROOT", vault)
        monkeypatch.setattr("llm_client.call_llm", lambda *args, **kwargs: f"# Page\n\n{secret}")
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        function(page, apply=True)
    elif module_name == "session_end_project_tag":
        monkeypatch.setattr("daily_log_append.append_knowledge", boundary)
        function(vault / "knowledge/daily/2026-07-15.md", secret, "event-1")
    elif module_name == "session_start_project_state":
        project = tmp_path / "project"
        (project / ".git").mkdir(parents=True)
        template = vault / "knowledge/projects/_template/state.md"
        template.parent.mkdir(parents=True)
        template.write_text("# <Project Name>\n- Project root: `<absolute-path>`\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
        monkeypatch.setattr(module, "ProjectStore", lambda *args: object())
        monkeypatch.setattr(module, "recover_project_handoff", lambda *args, **kwargs: SimpleNamespace(context="", degraded=False, legacy=False))
        monkeypatch.setattr(module, "mutate_knowledge", boundary)
        function()
    elif module_name == "tool_breadcrumb_append":
        monkeypatch.setattr("daily_log_append.append_daily", boundary)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
            "slug": "demo", "sessionId": "s1", "tool": "bash", "target": secret,
        })))
        function()
    elif module_name == "user_prompt_capture":
        monkeypatch.setattr("daily_log_append.append_daily", boundary)
        function("demo", "s1", secret, "event-1")
    else:
        raise AssertionError(entrypoint)

    assert calls, f"{entrypoint} did not delegate to the mutation boundary"
    if module_name not in {"archive_stale", "flush_memory", "migrate_to_okf", "rebuild_memory_index"}:
        assert secret not in repr(calls), f"{entrypoint} leaked a secret before prepare"


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
        'from markdown_transaction import mutate_knowledge\n'
        'p = ROOT / "knowledge" / "feedback" / "abcdef123456.json"\n'
        'mutate_knowledge("event", {p: b"x"})\n'
    )
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("mutate_knowledge", True)]


def test_python_scanner_traces_parameter_sink_alias_and_path_alias():
    from check_knowledge_writers import scan_source

    source = '''
from pathlib import Path as P
ROOT_DIR = P("knowledge")
def raw_write(destination):
    destination.write_text("x")
writer = raw_write
target = ROOT_DIR / "notes" / "x.md"
writer(target)
'''
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("raw_write", False, "<module>")
    ]


def test_python_scanner_traces_method_path_parameter_to_callsite():
    from check_knowledge_writers import scan_source

    source = '''
from pathlib import Path
class Writer:
    def save(self, destination):
        destination.write_bytes(b"x")
target = Path("knowledge/notes/x.md")
Writer().save(target)
'''
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("save", False, "<module>")
    ]


@pytest.mark.parametrize(
    ("source", "approved"),
    [
        (
            'from scripts.markdown_transaction import mutate_knowledge as mutate\n'
            'p = Path("knowledge/notes/x.md")\nmutate("id", {p: b"x"})\n',
            True,
        ),
        (
            'def mutate_knowledge(operation_id, changes):\n    return None\n'
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

    source = '''
import scripts.markdown_transaction as transactions
from pathlib import Path as P
safe_mutate = transactions.mutate_knowledge
target = P("knowledge") / "notes" / "x.md"
safe_mutate("id", {target: b"x"})
'''
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("safe_mutate", True)
    ]


@pytest.mark.parametrize(
    ("suffix", "source", "expected_api"),
    [
        (".js", 'const p = root + "/knowledge/notes/x.md";\nfs.writeFileSync(p, data);\n', "writeFileSync"),
        (".ps1", '$p = Join-Path $root "knowledge/notes/x.md"\nSet-Content -Path $p -Value $x\n', "Set-Content"),
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
        (".js", '// fs.writeFileSync("knowledge/notes/x.md", x)\nconst s = "writeFile knowledge/notes/x.md";\n'),
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
            'fs.writeFileSync(\n  target,\n  data\n);\n',
            "writeFileSync",
        ),
        (
            ".ps1",
            '$target = Join-Path `\n  $root `\n  "knowledge/notes/x.md"\n'
            'Set-Content `\n  -Path $target `\n  -Value $data\n',
            "Set-Content",
        ),
        (
            ".sh",
            'target="$root/knowledge/notes/x.md"\n'
            'printf "%s" \\\n  "$data" \\\n  > "$target"\n',
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
        markdown_transaction.append_knowledge(
            f"daily:event-{index}", daily, f"D{index:02d}-start\nD{index:02d}-end\n".encode()
        )
        markdown_transaction.append_knowledge(
            f"project:event-{index}", jsonl, f'{{"id":{index}}}\n'.encode()
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(12)))

    daily_text = daily.read_text(encoding="utf-8")
    jsonl_text = jsonl.read_text(encoding="utf-8")
    for index in range(12):
        assert daily_text.count(f"D{index:02d}-start\nD{index:02d}-end\n") == 1
        assert jsonl_text.count(f'{{"id":{index}}}\n') == 1


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
def test_mutation_recovers_after_crash_at_prepared_state(
    tmp_path, monkeypatch, relative, content
):
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
        markdown_transaction.mutate_knowledge(
            f"crash:{relative}", {target: content}
        )

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", original)
    record = markdown_transaction.mutate_knowledge(
        f"crash:{relative}", {target: content}
    )
    assert record.state == "committed"
    assert target.exists()


@pytest.mark.parametrize(("relative", "content"), WRITER_TARGETS)
def test_mutation_recovers_after_crash_during_apply(
    tmp_path, monkeypatch, relative, content
):
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
        markdown_transaction.mutate_knowledge(
            f"apply-crash:{relative}", {target: content}
        )
    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", original)
    record = markdown_transaction.mutate_knowledge(
        f"apply-crash:{relative}", {target: content}
    )
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
        markdown_transaction.mutate_knowledge(
            f"external:{relative}", {target: content}
        )
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
        markdown_transaction.append_knowledge(
            "project:escape", link / "events.jsonl", b"{}\n"
        )
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


def test_mutate_rejects_rebound_committed_operation(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "notes" / "page.md"
    markdown_transaction.mutate_knowledge("replay:event", {path: b"one"})
    with pytest.raises(ValueError, match="different request"):
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

    source = '''
def _build_bag_contents():
    hidden_build = archive_root / ".bag-building"
    (hidden_build / "bagit.txt").write_bytes(data)
def _publish_build():
    hidden_build.replace(final_bag)
def _bad_flat_write():
    flat = ROOT / "knowledge" / "daily" / "2026-01-01.md"
    flat.write_bytes(data)
'''
    findings = scan_source(Path("scripts/archive_daily.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("write_bytes", True),
        ("replace", True),
        ("write_bytes", False),
    ]
