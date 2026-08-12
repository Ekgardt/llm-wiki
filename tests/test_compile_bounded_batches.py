from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

_SYNTHETIC_PROJECT_SLUG = "compile-test"
_SYNTHETIC_PROJECT_ROOT = str(Path.cwd().resolve())


def _daily(path: Path, blocks: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Daily\n\n" + "\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def _block(timestamp: str, body: str, kind: str = "session-end") -> str:
    return (
        f"## [{timestamp}] {kind} | session\n"
        "- Trigger: `synthetic-test`\n"
        f"- Project slug: `{_SYNTHETIC_PROJECT_SLUG}`\n"
        f"- Project root JSON: {json.dumps(_SYNTHETIC_PROJECT_ROOT)}\n"
        "- Tier: `major`\n"
        "- Source session: `session`\n\n"
        "**Lessons / patterns**\n"
        f"- Rule: {body}\n"
        "<!-- llm-wiki-record-complete -->"
    )


def _generated_block(
    timestamp: str,
    root: Path,
    body: str,
    kind: str = "opencode-idle",
) -> str:
    return _project_block(
        timestamp,
        root,
        "admission-test",
        body,
        kind=kind,
        synthetic_rule=False,
    )


def _project_block(
    timestamp: str,
    root: Path,
    slug: str,
    body: str,
    *,
    kind: str = "opencode-idle",
    session: str = "session",
    synthetic_rule: bool = True,
) -> str:
    if synthetic_rule:
        body = "\n".join(
            f"{line[: len(line) - len(line.lstrip())]}- Rule: {line.lstrip()[2:]}"
            if line.lstrip().startswith("- ")
            else line
            for line in body.splitlines()
        )
    return (
        f"## [{timestamp}] {kind} | {session}\n"
        "- Trigger: `opencode-idle`\n"
        f"- Project slug: `{slug}`\n"
        f"- Project root JSON: {json.dumps(str(root.resolve()))}\n"
        "- Tier: `major`\n"
        f"- Source session: `{session}`\n\n"
        f"{body}\n"
        "<!-- llm-wiki-record-complete -->"
    )


def _word_body(count: int) -> str:
    return " ".join(f"durable{index}" for index in range(count))


def _valid_body(marker: str) -> str:
    return f"{marker}\n\n{_word_body(150)}"


def _exact_test_evidence_quote(
    daily: Path,
    timestamp: str,
    quoted_text: str,
) -> str:
    """Match the synthetic `Rule:` prefix when it is the complete source bullet."""
    try:
        lines = daily.read_text(encoding="utf-8").splitlines()
    except OSError:
        return quoted_text
    header_prefix = f"## [{timestamp}] "
    in_record = False
    for line in lines:
        if line.startswith("## ["):
            in_record = line.startswith(header_prefix)
            continue
        if not in_record:
            continue
        if line == f"- {quoted_text}":
            return quoted_text
        if line == f"- Rule: {quoted_text}":
            return f"Rule: {quoted_text}"
    return quoted_text


def _operation(daily: Path, slug: str, body: str, *, timestamp: str = "12:00:00") -> dict:
    return {
        "action": "create",
        "category": "patterns",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "summary": "Durable summary.",
        "body_section": "Lesson",
        "body_markdown": _valid_body(body),
        "evidence": [{
            "daily_date": daily.stem,
            "timestamp": timestamp,
            "quoted_text": _exact_test_evidence_quote(
                daily,
                timestamp,
                "journal evidence",
            ),
            "claim": "journal claim",
        }],
        "related": [],
    }


def _admission_operation(
    daily: Path,
    slug: str,
    quoted_text: str,
    *,
    action: str = "create",
    title: str | None = None,
    summary: str = "A distinct durable summary.",
    body: str | None = None,
    timestamp: str = "12:00:00",
) -> dict:
    return {
        "action": action,
        "category": "patterns",
        "slug": slug,
        "title": title or slug.replace("-", " ").title(),
        "summary": summary,
        "body_section": "Lesson",
        "body_markdown": body or _word_body(150),
        "evidence": [
            {
                "daily_date": daily.stem,
                "timestamp": timestamp,
                "quoted_text": _exact_test_evidence_quote(
                    daily,
                    timestamp,
                    quoted_text,
                ),
                "claim": "Durable admission evidence.",
            }
        ],
        "related": [],
    }


def _response(*operations: dict, audit: dict | None = None) -> str:
    return json.dumps({"operations": list(operations), "audit": audit or {}})


def _synthetic_current_journal(
    compile_memory,
    batch_id: str,
    sequence: int = 0,
    *,
    status: str = "complete",
) -> dict:
    accepted = {
        "operations": [],
        "audit": {
            "verified": sequence,
            "dedup": 0,
            "stubs": 0,
            "contradictions": 0,
            "rejected": 0,
        },
        "response_sha256": hashlib.sha256(f"response:{sequence}".encode()).hexdigest(),
        "source": [],
        "source_blocks": [],
        "batch_ids": [batch_id],
        "generation_id": hashlib.sha256(f"generation:{sequence}".encode()).hexdigest(),
        "layout_sha256": hashlib.sha256(f"layout:{sequence}".encode()).hexdigest(),
        "consumed_evidence": [],
    }
    journal = {
        "version": compile_memory.CURRENT_JOURNAL_VERSION,
        "batch_id": batch_id,
        "accepted": accepted,
        "accepted_sha256": compile_memory._canonical_digest(accepted),
        "operation_states": [],
        "operation_recovery": [],
        "operation_effects": [],
        "status": status,
    }
    return journal


def _completed_journal_bytes(compile_memory, batch_id: str, sequence: int = 0) -> bytes:
    journal = _synthetic_current_journal(compile_memory, batch_id, sequence)
    journal["journal_sha256"] = compile_memory._journal_digest(journal)
    return json.dumps(journal, ensure_ascii=False).encode("utf-8")


def _retired_archive_component(compile_env, monkeypatch, suffix: str):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 0)
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-11-{suffix}.md",
        [_block("12:00:00", f"Cold archive component {suffix} is disconnected.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        request, '{"operations": [], "audit": {}}', False
    )["ok"]
    [manifest_path] = (state_root / "run" / "retired-manifests").glob(
        f"{request['generation_id']}.*.json"
    )
    [journal_path] = (state_root / "run" / "retired-journals").glob(
        f"{request['batch_id']}.*.json"
    )
    for field in (
        "compile_generation_active",
        "compile_generation_completed",
        "compile_sdk_progress",
        "compile_sdk_wave",
        "compile_index_pending",
        "compile_daily_replay_boundaries",
        "compiled_daily_hashes",
        "compiled_daily_receipts",
        "compile_daily_checkpoints",
    ):
        state.pop(field, None)
    return request, manifest_path, journal_path


def _bind_archive_state_snapshot(compile_memory, monkeypatch, state):
    def snapshot():
        raw = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        return raw, json.loads(json.dumps(state))

    monkeypatch.setattr(compile_memory, "read_state_snapshot_strict", snapshot)


def _approve_archive_manifest(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["approved"] = True
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_note(
    path: Path,
    title: str,
    summary: str,
    *,
    status: str = "active",
    project_slug: str | None = None,
    project_root: Path | str | None = None,
    scoped: bool = True,
) -> None:
    if scoped and project_slug is None and project_root is None:
        project_slug = _SYNTHETIC_PROJECT_SLUG
        project_root = _SYNTHETIC_PROJECT_ROOT
    if not scoped and (project_slug is not None or project_root is not None):
        raise ValueError("unscoped test note cannot define project identity")
    if (project_slug is None) != (project_root is None):
        raise ValueError("test project identity must be complete")
    project_fields = (
        f"project: {project_slug}\n"
        f"project_root: {json.dumps(str(Path(project_root).resolve()))}\n"
        if project_slug is not None and project_root is not None
        else ""
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: pattern\n"
        f"status: {status}\n"
        f"{project_fields}"
        "---\n\n"
        f"# {title}\n\n"
        f"One-sentence summary: {summary}\n\n"
        "## Lesson\nExisting durable body.\n",
        encoding="utf-8",
    )


def _hard_link_or_skip(source: Path, alias: Path) -> None:
    try:
        os.link(source, alias)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"native file hard links are unavailable: {exc}")
    if source.stat().st_nlink < 2:
        pytest.skip("native file hard-link count is unavailable")


def _directory_link_or_skip(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            pytest.skip(f"directory junctions are unavailable: {detail}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")


def _assert_rejected_before_journal(
    result: dict,
    request: dict,
    daily: Path,
    state_root: Path,
    state: dict,
) -> None:
    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()


class _BoundedOnlyInput(io.StringIO):
    def __init__(self, value: str):
        super().__init__(value)
        self.request_sizes: list[int] = []

    def read(self, size: int = -1) -> str:
        self.request_sizes.append(size)
        assert size > 0, "reader requested an unbounded allocation"
        return super().read(size)


def _rebuild_test_index(compile_memory) -> bool:
    links = [
        f"- [[knowledge/notes/{path.stem}]]"
        for path in sorted(compile_memory.KNOWLEDGE.glob("*.md"))
    ]
    compile_memory.INDEX.write_text("\n".join(links) + "\n", encoding="utf-8")
    return True


@pytest.fixture
def compile_env(tmp_path, monkeypatch):
    import compile_memory

    root = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    (state_root / "run").mkdir(parents=True)
    state: dict = {}

    def load_state():
        return json.loads(json.dumps(state))

    def update_state(mutator):
        mutator(state)
        return state

    monkeypatch.setattr(compile_memory, "ROOT", root)
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(compile_memory, "DAILY_DIR", root / "knowledge" / "daily")
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", root / "knowledge" / "notes")
    monkeypatch.setattr(compile_memory, "INDEX", root / "knowledge" / "index.md")
    monkeypatch.setattr(compile_memory, "AGENTS", root / "AGENTS.md")
    monkeypatch.setattr(compile_memory, "LOG", root / "knowledge" / "log.md")
    monkeypatch.setattr(compile_memory, "load_state", load_state)
    monkeypatch.setattr(compile_memory, "update_state", update_state)
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )
    monkeypatch.setenv("MEMORY_COMPILE_PROMPT_CHAR_BUDGET", "30000")
    return compile_memory, root, state_root, state


def test_noise_backlog_is_filtered_before_prompt_construction(compile_env):
    compile_memory, root, state_root, state = compile_env
    noise = "- `[10:00:00] tool | session | demo | Edit` \n" * 35_000
    noise += "x" * 100_000  # total input is over 1.5 million characters
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-20.md",
        [
            noise,
            "## [10:01:00] opencode-idle | session\n"
            "- Tier: `major`\n\n"
            "(no body)",
            _block("10:02:00", "The durable retry rule keeps evidence pending."),
        ],
    )
    assert len(daily.read_text(encoding="utf-8")) > 1_500_000

    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=40_000
    )

    assert request["pending"] is True
    assert "durable retry rule" in request["prompt"]
    assert "tool | session" not in request["prompt"]
    assert "(no body)" not in request["prompt"]
    assert len(request["prompt"]) + len(request["system_prompt"]) <= 40_000


def test_empty_generation_does_not_publish_before_successful_index_barrier(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-20-empty.md",
        [],
    )
    rebuilds: list[int] = []
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: rebuilds.append(1) and False,
    )

    with pytest.raises(compile_memory.CompilePreparationError, match="index rebuild failed"):
        compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )

    assert rebuilds == [1]
    assert daily.name not in state.get("compiled_daily_hashes", {})
    pending = state["compile_index_pending"]
    assert pending["generation_id"] == state["compile_generation_active"][daily.name][
        "generation_id"
    ]
    assert "batch_id" not in pending


def test_multiple_dailies_are_batched_under_configured_total_budget(compile_env):
    compile_memory, root, _state_root, state = compile_env
    dailies = [
        _daily(
            root / "knowledge" / "daily" / f"2026-07-{day}.md",
            [_block(f"0{index}:00:00", f"Evidence {day}-{index} " + "z" * 14_000)
             for index in range(1, 4)],
        )
        for day in (20, 21)
    ]
    os.environ["MEMORY_COMPILE_PROMPT_CHAR_BUDGET"] = "32000"
    requests = []
    while True:
        request = compile_memory.prepare_compile_request(
            dailies, state, prompt_char_budget=32_000
        )
        if not request["pending"]:
            break
        requests.append(request)
        result = compile_memory.apply_compile_batch(
            request,
            '{"operations": [], "audit": {}}',
            dry_run=False,
        )
        assert result["ok"] is True

    assert len(requests) >= 4
    assert {item["dailies"][0]["path"] for item in requests} == {
        "knowledge/daily/2026-07-20.md",
        "knowledge/daily/2026-07-21.md",
    }
    assert all(
        len(item["prompt"]) + len(item["system_prompt"]) <= 32_000
        for item in requests
    )
    assert set(state["compiled_daily_hashes"]) == {
        "2026-07-20.md",
        "2026-07-21.md",
    }


def test_oversized_meaningful_block_remains_pending_and_fails_visibly(compile_env):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-22.md",
        [_block("12:00:00", "evidence " + "x" * 50_000)],
    )

    with pytest.raises(compile_memory.CompilePreparationError, match="exceeds"):
        compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=20_000
        )

    assert daily.name not in state.get("compiled_daily_hashes", {})
    error = state["last_compile_sdk_error"]
    assert error["stage"] == "prepare"
    assert daily.name in error["error"]
    assert "stage=prepare" in (
        state_root / "logs" / "compile-sdk-last.log"
    ).read_text(encoding="utf-8")


def test_partial_batch_failure_does_not_publish_daily_hash(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-23.md",
        [
            _block("12:00:00", "first evidence " + "a" * 14_000),
            _block("12:01:00", "second evidence " + "b" * 14_000),
        ],
    )
    os.environ["MEMORY_COMPILE_PROMPT_CHAR_BUDGET"] = "24000"
    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    assert compile_memory.apply_compile_batch(
        first, '{"operations": [], "audit": {}}', False
    )["ok"]
    second = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )

    result = compile_memory.apply_compile_batch(second, "provider garbage", False)

    assert result["ok"] is False
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert state["last_compile_sdk_error"]["stage"] == "validate"


def test_concurrent_append_finishes_active_generation_then_queues_append(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-24.md",
        [_block("12:00:00", "original evidence")],
    )
    original_hash = compile_memory._daily_snapshot_hash(daily)
    real_pack = compile_memory._pack_daily_blocks
    appended = False

    def append_during_prepare(
        path,
        budget,
        blocks,
        context_snapshot=None,
        source_hash=None,
    ):
        nonlocal appended
        if not appended:
            appended = True
            daily.write_text(
                daily.read_text(encoding="utf-8")
                + "\n"
                + _block("12:01:00", "concurrent evidence")
                + "\n",
                encoding="utf-8",
            )
        return real_pack(path, budget, blocks, context_snapshot, source_hash)

    monkeypatch.setattr(compile_memory, "_pack_daily_blocks", append_during_prepare)
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert request["dailies"][0]["sha256"] == original_hash
    manifest = compile_memory._load_manifest(request["generation_id"])
    stored = compile_memory._request_from_manifest(manifest, request["batch_id"])
    assert request == stored
    assert compile_memory._manifest_source_available(manifest, request["source_blocks"])

    result = compile_memory.apply_compile_batch(
        request, '{"operations": [], "audit": {}}', False
    )

    assert result["ok"] is True
    assert state["compiled_daily_hashes"][daily.name] == request["dailies"][0]["sha256"]

    queued = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert queued["pending"] is True
    assert queued["generation_id"] != request["generation_id"]
    assert len(queued["source_blocks"]) == 1
    assert "concurrent evidence" in queued["source_blocks"][0]


def test_direct_compile_does_not_stamp_append_after_provider_returns(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-24.md",
        [_block("12:00:00", "original evidence")],
    )

    def append_before_return(_paths, _dry_run):
        daily.write_text(
            daily.read_text(encoding="utf-8")
            + "\n"
            + _block("12:01:00", "late evidence")
            + "\n",
            encoding="utf-8",
        )
        return [], "COMPILE_DONE: 0 page(s) touched"

    monkeypatch.setattr(compile_memory, "run_compile", append_before_return)
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )
    monkeypatch.setattr(compile_memory, "append_log", lambda _entry: None)
    args = argparse.Namespace(
        trigger="manual", dry_run=False, file=None, all=False, sdk_paths=None
    )
    monkeypatch.setattr(
        compile_memory, "select_dailies", lambda _args, _state: [daily]
    )

    assert compile_memory._run(args) == 0
    assert daily.name not in state.get("compiled_daily_hashes", {})


def test_retry_is_idempotent_and_does_not_apply_operations_twice(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-25.md",
        [_block("12:00:00", "retry evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = '{"operations": [], "audit": {}}'
    first = compile_memory.apply_compile_batch(request, response, False)
    retry = compile_memory.apply_compile_batch(request, response, False)

    assert first["status"] == "applied"
    assert retry == {"ok": True, "status": "already_applied"}


def test_provider_failure_recorder_persists_state_and_log(compile_env):
    compile_memory, _root, state_root, state = compile_env

    compile_memory.record_sdk_failure("provider", "Luna unavailable", "batch-7")

    assert state["last_compile_sdk_error"] == {
        "stage": "provider",
        "error": "Luna unavailable",
        "batch_id": "batch-7",
        "at": state["last_compile_sdk_error"]["at"],
    }
    log = (state_root / "logs" / "compile-sdk-last.log").read_text(encoding="utf-8")
    assert "stage=provider" in log
    assert "batch=batch-7" in log
    assert "Luna unavailable" in log


@pytest.mark.parametrize(
    ("flag", "payload"),
    (
        (
            "--record-sdk-failure",
            {"stage": "provider", "error": "x" * 512},
        ),
        (
            "--apply-sdk-response",
            {"request": {}, "response": "x" * 512},
        ),
    ),
)
def test_compile_bridge_rejects_oversized_stdin_before_state_or_apply(
    compile_env,
    monkeypatch,
    flag,
    payload,
):
    compile_memory, _root, state_root, state = compile_env
    downstream_calls: list[tuple] = []
    stream = _BoundedOnlyInput(json.dumps(payload))
    initial_state = json.loads(json.dumps(state))
    monkeypatch.setattr(
        compile_memory,
        "record_sdk_failure",
        lambda *args: downstream_calls.append(("record", args)),
    )
    monkeypatch.setattr(
        compile_memory,
        "apply_compile_batch",
        lambda *args: downstream_calls.append(("apply", args)) or {"ok": True},
    )
    monkeypatch.setattr(
        compile_memory,
        "MAX_SDK_BRIDGE_STDIN_BYTES",
        128,
        raising=False,
    )
    monkeypatch.setattr(sys, "argv", ["compile_memory.py", flag])
    monkeypatch.setattr(sys, "stdin", stream)

    assert compile_memory.main() == 2
    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert downstream_calls == []
    assert state == initial_state
    assert not (state_root / "logs" / "compile-sdk-last.log").exists()


def test_prompt_budget_has_safe_default_and_rejects_invalid_values(monkeypatch):
    import compile_memory

    monkeypatch.delenv("MEMORY_COMPILE_PROMPT_CHAR_BUDGET", raising=False)
    assert compile_memory.prompt_char_budget() == 120_000
    monkeypatch.setenv("MEMORY_COMPILE_PROMPT_CHAR_BUDGET", "0")
    with pytest.raises(ValueError, match="positive integer"):
        compile_memory.prompt_char_budget()


def test_compile_prompt_requires_complete_normalized_bullet_citations(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-26-prompt-bullet.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Lessons / patterns**\n"
                "- Always cite the complete normalized evidence bullet.",
            )
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    request = compile_memory.build_compile_request(
        [daily],
        daily_blocks=source_blocks,
        prompt_char_budget=30_000,
    )
    prompt = " ".join(request["prompt"].split())

    assert "must exactly equal the complete text of one displayed source bullet" in prompt
    assert "excluding its leading `- ` marker" in prompt
    assert "exact source substring" not in prompt
    lower_prompt = prompt.lower()
    assert "related must be a json array of strings; use [] when empty" in lower_prompt
    assert "count body_markdown words before returning each create" in lower_prompt
    assert "title, summary, evidence, and related do not count" in lower_prompt


def test_evidence_quote_rejects_altered_whitespace(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-26.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Lessons / patterns**\n- Exact   spacing continues here",
            )
        ],
    )
    evidence = [
        {
            "daily_date": daily.stem,
            "timestamp": "12:00:00",
            "quoted_text": "Exact spacing continues here",
        }
    ]

    assert compile_memory._verify_evidence(evidence, [daily]) == (0, 1)


def test_evidence_quote_accepts_complete_normalized_bullet(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Always preserve visible text around inline comments."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-26-normalized-bullet.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Lessons / patterns**\n"
                "- Always preserve <!-- hidden detail -->visible text around "
                "inline comments.",
            )
        ],
    )
    evidence = [
        {
            "daily_date": daily.stem,
            "timestamp": "12:00:00",
            "quoted_text": quote,
        }
    ]

    assert compile_memory._verify_evidence(evidence, [daily]) == (1, 0)


def test_evidence_quote_rejects_substring_of_complete_bullet(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-26-substring-decoy.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Lessons / patterns**\n"
                "- Always reject substring citations before durable publication.",
            )
        ],
    )
    evidence = [
        {
            "daily_date": daily.stem,
            "timestamp": "12:00:00",
            "quoted_text": "reject substring citations",
        }
    ]

    assert compile_memory._verify_evidence(evidence, [daily]) == (0, 1)


def test_evidence_quote_matches_later_repeated_timestamp_block(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Always keep later evidence with its repeated timestamp block."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-26-repeated-timestamp.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Lessons / patterns**\n- Always keep earlier evidence distinct.",
            ),
            _generated_block(
                "12:00:00",
                root,
                f"**Lessons / patterns**\n- {quote}",
            ),
        ],
    )
    evidence = [
        {
            "daily_date": daily.stem,
            "timestamp": "12:00:00",
            "quoted_text": quote,
        }
    ]

    assert compile_memory._verify_evidence(evidence, [daily]) == (1, 0)


def test_nondurable_substring_decoy_does_not_poison_exact_durable_bullet(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Always require complete bullet identity before admitting evidence."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-26-quality-decoy.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Work completed**\n"
                f"- Status says {quote} This is a longer operational bullet.\n\n"
                "**Lessons / patterns**\n"
                f"- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "quality-decoy", quote)),
        False,
    )

    assert result["ok"] is True, result


def test_exact_durable_and_nondurable_duplicate_remains_ambiguous(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Always keep exact duplicate quality visible during admission."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-26-quality-duplicate.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Work completed**\n"
                f"- {quote}\n\n"
                "**Lessons / patterns**\n"
                f"- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "quality-duplicate", quote)),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "ambiguous between durable and nondurable" in result["error"]


def test_plain_opencode_idle_audit_status_citation_is_rejected_before_journal(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Audit complete: twelve files changed and all checks passed."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-01.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"{quote}\n\n**Lessons / patterns**\n"
                "- Validate immutable source evidence before publication.",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    target = root / "knowledge" / "notes" / "plain-status.md"

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "plain-status", quote)),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "not an exact source citation" in result["error"]
    assert not target.exists()


def test_model_counters_cannot_admit_status_disguised_as_lesson(compile_env):
    compile_memory, root, state_root, state = compile_env
    status = "Status: implementation complete; twelve files changed."
    durable = "When status is transient, exclude it before compiling evidence."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-disguised-status.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"**Lessons / patterns**\n- {status}\n- {durable}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(daily, "disguised-status", status),
            audit={"verified": 999, "dedup": 999},
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "durable section" in result["error"]


@pytest.mark.parametrize(
    "outside_quote",
    (
        "Status: implementation complete; twelve files changed.",
        "Files changed: scripts/compiler.py and tests/test_compiler.py.",
        "Path/code summary: scripts/compiler.py now calls validate_source().",
    ),
)
def test_nondurable_citation_cannot_update_even_with_valid_durable_sibling(
    compile_env,
    outside_quote,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-01-update.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"{outside_quote}\n\n**Lessons / patterns**\n"
                "- Retry the immutable journal before requesting a replacement plan.",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "status-update.md"
    _write_note(target, "Status Update", "The existing durable note stays unchanged.")
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                outside_quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "not an exact source citation" in result["error"]
    assert target.read_bytes() == before


def test_citation_cannot_borrow_durable_occurrence_from_another_daily(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "The same text has different admission quality on another date."
    first = _daily(
        root / "knowledge" / "daily" / "2026-09-01-first.md",
        [
            _project_block(
                "12:00:00",
                root / "projects" / "alpha",
                "alpha",
                f"{quote}\n\n**Lessons / patterns**\n"
                "- A separate valid lesson keeps this record compile-eligible.",
                session="alpha-session",
            )
        ],
    )
    second = _daily(
        root / "knowledge" / "daily" / "2026-09-01-second.md",
        [
            _project_block(
                "12:00:00",
                root / "projects" / "beta",
                "beta",
                f"**Lessons / patterns**\n- {quote}",
                session="beta-session",
            )
        ],
    )
    source_blocks = [
        *compile_memory.extract_meaningful_blocks(first.read_text(encoding="utf-8")),
        *compile_memory.extract_meaningful_blocks(second.read_text(encoding="utf-8")),
    ]

    plan, error = compile_memory._normalize_accepted_plan(
        _response(_admission_operation(first, "date-scoped-citation", quote)),
        [first, second],
        source_blocks,
    )

    assert plan is None
    assert "not an exact source citation" in error


def test_structured_idle_lesson_is_accepted(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Retry the immutable journal before requesting another provider plan."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(daily, "structured-idle-lesson", quote)

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation, audit={"verified": 900, "dedup": 800}),
        False,
    )

    assert result["ok"] is True
    target = root / "knowledge" / "notes" / "structured-idle-lesson.md"
    content = target.read_text(encoding="utf-8")
    assert 'project: "admission-test"\n' in content
    assert f"project_root: {json.dumps(str(root.resolve()))}\n" in content
    assert compile_memory.parse_compile_audit(result["audit"])["verified"] == 1
    assert compile_memory.parse_compile_audit(result["audit"])["dedup"] == 3


@pytest.mark.parametrize("project_slug", ("true", "null", "123"))
def test_created_project_identity_is_json_quoted_yaml_string(
    compile_env,
    project_slug,
):
    import yaml

    compile_memory, root, _state_root, state = compile_env
    quote = "Validate project identity before serializing frontmatter."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-09-02-{project_slug}.md",
        [
            _project_block(
                "12:00:00",
                root,
                project_slug,
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, f"identity-{project_slug}", quote)),
        False,
    )

    assert result["ok"] is True
    content = (
        root / "knowledge" / "notes" / f"identity-{project_slug}.md"
    ).read_text(encoding="utf-8")
    frontmatter = content.split("---", 2)[1]
    parsed = yaml.safe_load(frontmatter)
    assert f"project: {json.dumps(project_slug)}\n" in content
    assert f"project_root: {json.dumps(str(root.resolve()))}\n" in content
    assert parsed["project"] == project_slug
    assert isinstance(parsed["project"], str)
    assert parsed["project_root"] == str(root.resolve())
    assert isinstance(parsed["project_root"], str)


def test_compile_operation_project_identity_is_derived_from_cited_source(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Project provenance is selected by validated source evidence."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-derived.md",
        [
            _project_block(
                "12:00:00",
                root,
                "derived-project",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    provider_operation = _admission_operation(daily, "derived-project-note", quote)

    plan, error = compile_memory._normalize_accepted_plan(
        _response(provider_operation),
        [daily],
        source_blocks,
    )

    assert error == ""
    assert plan is not None
    assert "project_slug" not in provider_operation
    assert "project_root" not in provider_operation
    assert plan["operations"][0]["project_slug"] == "derived-project"
    assert plan["operations"][0]["project_root"] == str(root.resolve())

    forged = json.loads(json.dumps(provider_operation))
    forged["project_slug"] = "forged-project"
    forged["project_root"] = str((root / "forged").resolve())
    forged_plan, forged_error = compile_memory._normalize_accepted_plan(
        _response(forged),
        [daily],
        source_blocks,
    )
    assert forged_plan is None
    assert "unknown fields" in forged_error


def test_matching_project_update_is_accepted(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "A project note may be updated only by evidence from the same project."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-matching-update.md",
        [
            _project_block(
                "12:00:00",
                root,
                "matching-project",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "matching-project-note.md"
    _write_note(
        target,
        "Matching Project Note",
        "The existing project identity remains authoritative.",
        project_slug="matching-project",
        project_root=root,
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    assert result["ok"] is True, result
    assert target.read_text(encoding="utf-8").count("## Update") == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
def test_matching_project_update_accepts_equivalent_windows_root_spelling(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Use host-native path comparison to preserve equivalent Windows roots."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-equivalent-root.md",
        [
            _project_block(
                "12:00:00",
                root,
                "matching-project",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "equivalent-root-note.md"
    _write_note(
        target,
        "Equivalent Root Note",
        "Equivalent native roots retain the same project identity.",
        project_slug="matching-project",
        project_root=root,
    )
    canonical = str(root.resolve())
    equivalent = canonical.swapcase().replace("\\", "/")
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            json.dumps(canonical),
            json.dumps(equivalent),
        ),
        encoding="utf-8",
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    assert result["ok"] is True, result
    assert target.read_text(encoding="utf-8").count("## Update") == 1


def test_admitted_source_index_extracts_each_record_once_without_quote_rescans(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, _state = compile_env
    records = (
        ("12:00:00", "Validate source evidence once before publication."),
        ("12:01:00", "Use one admitted index to preserve project identity."),
        ("12:02:00", "Reject mutable evidence before durable publication."),
        ("12:03:00", "Keep source provenance immutable after admission."),
        ("12:04:00", "Always retain normalized evidence identity in the index."),
        ("12:05:00", "Ensure each distinct citation has one indexed fact."),
    )
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-linear-index.md",
        [
            _project_block(
                timestamp,
                root,
                "linear-project",
                f"**Lessons / patterns**\n- {quote}",
            )
            for timestamp, quote in records
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    operations = [
        _admission_operation(
            daily,
            f"linear-index-{index}",
            records[index][1],
            timestamp=records[index][0],
            summary=f"Distinct linear index summary {index}.",
        )
        for index in range(6)
    ]
    counters = {"build": 0, "render": 0, "extract": 0, "substring": 0}
    real_build = compile_memory._build_admitted_source_index
    real_render = compile_memory.render_daily_record_for_compile
    real_extract = getattr(
        compile_memory,
        "daily_record_evidence_occurrences",
        lambda _record: (),
    )
    real_substring = getattr(
        compile_memory,
        "_substring_positions",
        lambda *_args, **_kwargs: (),
    )

    def counted_build(*args, **kwargs):
        counters["build"] += 1
        return real_build(*args, **kwargs)

    def counted_render(*args, **kwargs):
        counters["render"] += 1
        return real_render(*args, **kwargs)

    def counted_extract(*args, **kwargs):
        counters["extract"] += 1
        return real_extract(*args, **kwargs)

    def counted_substring(*args, **kwargs):
        counters["substring"] += 1
        return real_substring(*args, **kwargs)

    monkeypatch.setattr(compile_memory, "_build_admitted_source_index", counted_build)
    monkeypatch.setattr(compile_memory, "render_daily_record_for_compile", counted_render)
    monkeypatch.setattr(
        compile_memory,
        "daily_record_evidence_occurrences",
        counted_extract,
        raising=False,
    )
    monkeypatch.setattr(
        compile_memory,
        "_substring_positions",
        counted_substring,
        raising=False,
    )

    plan, error = compile_memory._normalize_accepted_plan(
        _response(*operations),
        [daily],
        source_blocks,
    )

    assert error == ""
    assert plan is not None
    assert counters == {
        "build": 1,
        "render": len(records),
        "extract": len(records),
        "substring": 0,
    }


def test_source_block_matching_preserves_duplicate_multiplicity(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    record = _project_block(
        "12:00:00",
        root,
        "duplicate-project",
        "**Lessons / patterns**\n- Always count each duplicate source occurrence.",
    )
    single_source = f"# Daily\n\n{record}\n"
    duplicate_source = f"# Daily\n\n{record}\n\n{record}\n"
    [block] = compile_memory.extract_meaningful_blocks(single_source)
    required = [block, block]

    assert not compile_memory._canonical_blocks_available(single_source, required)
    with pytest.raises(ValueError, match="prepared batch"):
        compile_memory._build_admitted_source_index(
            {"2026-09-02": single_source},
            required,
        )

    assert compile_memory._canonical_blocks_available(duplicate_source, required)
    compile_memory._build_admitted_source_index(
        {"2026-09-02": duplicate_source},
        required,
    )


def test_source_block_availability_matching_has_linear_equality_work(monkeypatch):
    import compile_memory

    class Probe(str):
        equality_visits = 0
        hash_visits = 0

        def __hash__(self):
            type(self).hash_visits += 1
            return str.__hash__(self)

        def __eq__(self, other):
            type(self).equality_visits += 1
            return str.__eq__(self, other)

    count = 512
    available = [Probe(f"source-block-{index}") for index in range(count)]
    required = list(reversed(available))
    monkeypatch.setattr(
        compile_memory,
        "extract_meaningful_blocks",
        lambda _text: available,
    )

    assert compile_memory._canonical_blocks_available("ignored", required)
    assert Probe.equality_visits <= count * 4
    assert Probe.hash_visits <= count * 8


def test_apply_reuses_admitted_source_index_for_journal_and_receipt_validation(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Validate one admitted source index throughout the apply pass."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-linear-apply.md",
        [
            _project_block(
                "12:00:00",
                root,
                "linear-project",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    builds = 0
    real_build = compile_memory._build_admitted_source_index

    def counted_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(compile_memory, "_build_admitted_source_index", counted_build)

    def reject_substring_scan(*_args, **_kwargs):
        pytest.fail("exact admitted-source lookup must not rescan source strings")

    monkeypatch.setattr(
        compile_memory,
        "_substring_positions",
        reject_substring_scan,
        raising=False,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "linear-apply", quote)),
        False,
    )

    assert result["ok"] is True, result
    assert builds == 1


def test_multibatch_generation_caches_source_index_and_reserved_evidence_scan(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    records = tuple(
        (
            f"12:0{index}:00",
            f"Always retain one linear admission fact for generation batch {index}.",
        )
        for index in range(3)
    )
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-linear-generation.md",
        [
            _project_block(
                timestamp,
                root,
                "linear-project",
                (
                    "**Lessons / patterns**\n"
                    f"- {quote}\n\n"
                    "**Open questions**\n"
                    f"- Can {'x' * 14_000}{index} be retained?"
                ),
                synthetic_rule=False,
            )
            for index, (timestamp, quote) in enumerate(records)
        ],
    )
    first = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=24_000,
    )
    assert first["batch_count"] == len(records)

    builds = 0
    reserved_journal_loads = 0
    inside_reserved_scan = False
    real_build = compile_memory._build_admitted_source_index
    real_load_journal = compile_memory._load_journal
    real_reserved = compile_memory._reserved_evidence_for_manifest

    def counted_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        return real_build(*args, **kwargs)

    def counted_load_journal(*args, **kwargs):
        nonlocal reserved_journal_loads
        if inside_reserved_scan:
            reserved_journal_loads += 1
        return real_load_journal(*args, **kwargs)

    def counted_reserved(*args, **kwargs):
        nonlocal inside_reserved_scan
        assert inside_reserved_scan is False
        inside_reserved_scan = True
        try:
            return real_reserved(*args, **kwargs)
        finally:
            inside_reserved_scan = False

    monkeypatch.setattr(
        compile_memory,
        "_build_admitted_source_index",
        counted_build,
    )
    monkeypatch.setattr(compile_memory, "_load_journal", counted_load_journal)
    monkeypatch.setattr(
        compile_memory,
        "_reserved_evidence_for_manifest",
        counted_reserved,
    )

    request = first
    for index in range(len(records)):
        source = "\n".join(request["source_blocks"])
        [(timestamp, quote)] = [
            record for record in records if record[1] in source
        ]
        result = compile_memory.apply_compile_batch(
            request,
            _response(
                _admission_operation(
                    daily,
                    f"linear-generation-{index}",
                    quote,
                    timestamp=timestamp,
                    summary=f"Distinct generation admission summary {index}.",
                )
            ),
            False,
        )
        assert result["ok"] is True, result
        if index + 1 < len(records):
            progress = state["compile_sdk_progress"][daily.name]
            assert progress["generation_id"] == request["generation_id"]
            assert progress["consumed_evidence"]
            request = compile_memory.prepare_compile_request(
                [daily],
                state,
                prompt_char_budget=24_000,
            )

    assert builds == 1 and reserved_journal_loads <= len(records), (
        f"generation source builds={builds}, "
        f"reserved-evidence journal loads={reserved_journal_loads}"
    )


def test_failed_accepted_batch_remains_in_cached_evidence_barrier(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    first_quote = "Always persist the first accepted generation citation."
    reserved_quote = "Never release evidence from a failed accepted journal."
    records = (
        ("12:00:00", first_quote),
        ("12:01:00", reserved_quote),
        ("12:01:00", reserved_quote),
    )
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-failed-reservation.md",
        [
            _project_block(
                timestamp,
                root,
                "linear-project",
                (
                    "**Lessons / patterns**\n"
                    f"- {quote}\n\n"
                    "**Open questions**\n"
                    f"- Can {'y' * 14_000}{index} be retained?"
                ),
                synthetic_rule=False,
            )
            for index, (timestamp, quote) in enumerate(records)
        ],
    )
    first = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=24_000,
    )
    assert first["batch_count"] == len(records)
    first_operation = _admission_operation(
        daily,
        "failed-reservation-first",
        first_quote,
        timestamp="12:00:00",
    )
    first_result = compile_memory.apply_compile_batch(
        first,
        _response(first_operation),
        False,
    )
    assert first_result["ok"] is True
    progress = state["compile_sdk_progress"][daily.name]
    progress.pop("accepted_batch_ids")
    progress.pop("consumed_evidence")

    second = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=24_000,
    )
    real_execute = compile_memory._execute_plan

    def fail_after_journal(plan, *args, **kwargs):
        if any(
            operation.get("slug") == "failed-reservation-second"
            for operation in plan["operations"]
        ):
            raise OSError("injected post-journal failure")
        return real_execute(plan, *args, **kwargs)

    monkeypatch.setattr(compile_memory, "_execute_plan", fail_after_journal)
    second_operation = _admission_operation(
        daily,
        "failed-reservation-second",
        reserved_quote,
        timestamp="12:01:00",
        summary="The failed accepted plan owns this evidence token.",
    )
    second_result = compile_memory.apply_compile_batch(
        second,
        _response(second_operation),
        False,
    )
    assert second_result["ok"] is False
    assert second_result["status"] == "apply_failed"
    cached_evidence = state["compile_sdk_progress"][daily.name]["consumed_evidence"]
    assert compile_memory._evidence_token(first_operation["evidence"][0]) in cached_evidence
    assert compile_memory._evidence_token(second_operation["evidence"][0]) in cached_evidence
    monkeypatch.setattr(compile_memory, "_execute_plan", real_execute)

    manifest = compile_memory._load_manifest(first["generation_id"])
    third = compile_memory._request_from_manifest(
        manifest,
        manifest["batch_ids"][2],
    )
    result = compile_memory.apply_compile_batch(
        third,
        _response(
            _admission_operation(
                daily,
                "failed-reservation-third",
                reserved_quote,
                timestamp="12:01:00",
                summary="A later batch cannot reuse failed accepted evidence.",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, third, daily, state_root, state)
    assert "evidence was already consumed" in result["error"]


def test_project_source_cannot_update_unscoped_note(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Missing target provenance cannot be inferred from a model operation."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-missing-project.md",
        [
            _project_block(
                "12:00:00",
                root,
                "scoped-source",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "unscoped-target.md"
    _write_note(
        target,
        "Unscoped Target",
        "This note has no project provenance.",
        scoped=False,
    )
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "project identity" in result["error"]
    assert target.read_bytes() == before


def test_cross_project_update_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    alpha_root = (root / "projects" / "alpha").resolve()
    beta_root = (root / "projects" / "beta").resolve()
    quote = "Cross-project evidence must not mutate another project's note."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-cross-project.md",
        [
            _project_block(
                "12:00:00",
                alpha_root,
                "alpha",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "beta-note.md"
    _write_note(
        target,
        "Beta Note",
        "Beta owns this durable note.",
        project_slug="beta",
        project_root=beta_root,
    )
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "project identity does not match" in result["error"]
    assert target.read_bytes() == before


def test_matching_slug_with_changed_project_root_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    current_root = (root / "projects" / "current").resolve()
    changed_root = (root / "projects" / "moved").resolve()
    quote = "A changed canonical root is a different project identity."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-changed-root.md",
        [
            _project_block(
                "12:00:00",
                current_root,
                "stable-slug",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "stable-slug-note.md"
    _write_note(
        target,
        "Stable Slug Note",
        "The old canonical root remains recorded.",
        project_slug="stable-slug",
        project_root=changed_root,
    )
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "project identity does not match" in result["error"]
    assert target.read_bytes() == before


def test_mixed_project_sources_reject_whole_operation(compile_env):
    compile_memory, root, state_root, state = compile_env
    alpha_quote = "Alpha evidence remains scoped to alpha."
    beta_quote = "Beta evidence remains scoped to beta."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-mixed-project.md",
        [
            _project_block(
                "12:00:00",
                root / "projects" / "alpha",
                "alpha",
                f"**Lessons / patterns**\n- {alpha_quote}",
                session="alpha-session",
            ),
            _project_block(
                "12:01:00",
                root / "projects" / "beta",
                "beta",
                f"**Lessons / patterns**\n- {beta_quote}",
                session="beta-session",
            ),
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(daily, "mixed-project-note", alpha_quote)
    operation["evidence"].append(
        {
            "daily_date": daily.stem,
            "timestamp": "12:01:00",
            "quoted_text": _exact_test_evidence_quote(
                daily,
                "12:01:00",
                beta_quote,
            ),
            "claim": "Mixed provenance must fail closed.",
        }
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "mixed project" in result["error"]
    assert not (root / "knowledge" / "notes" / "mixed-project-note.md").exists()


def test_project_identity_is_validated_in_compile_receipt(compile_env):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    quote = "Receipt validation binds the durable effect to its source project."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-project-receipt.md",
        [
            _project_block(
                "12:00:00",
                root,
                "receipt-project",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "project-receipt", quote)),
        False,
    )

    assert result["ok"] is True
    receipt = state["compiled_daily_receipts"][daily.name]
    assert receipt["version"] == 2
    assert receipt["effects"][0]["project_slug"] == "receipt-project"
    assert receipt["effects"][0]["project_root"] == str(root.resolve())
    assert memory_state.is_compile_receipt_valid(
        receipt,
        daily.name,
        request["dailies"][0]["sha256"],
        root=root,
    )

    tampered = json.loads(json.dumps(receipt))
    tampered["effects"][0]["project_root"] = str((root / "moved").resolve())
    assert not memory_state.is_compile_receipt_valid(
        tampered,
        daily.name,
        request["dailies"][0]["sha256"],
        root=root,
    )

    downgraded = json.loads(json.dumps(receipt))
    downgraded["version"] = 1
    downgraded["effects"][0].pop("project_slug")
    downgraded["effects"][0].pop("project_root")
    assert not memory_state.is_compile_receipt_valid(
        downgraded,
        daily.name,
        request["dailies"][0]["sha256"],
        root=root,
    )


def test_rehashed_journal_project_tamper_is_rejected_against_source(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Journal replay re-derives project identity from immutable source."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-02-project-journal.md",
        [
            _project_block(
                "12:00:00",
                root,
                "journal-project",
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = _response(_admission_operation(daily, "project-journal", quote))
    first = compile_memory.apply_compile_batch(request, response, False)
    target = root / "knowledge" / "notes" / "project-journal.md"
    before = target.read_bytes()
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    operation = journal["accepted"]["operations"][0]
    original_fingerprint = compile_memory._operation_replay_fingerprint(operation)
    assert operation["project_slug"] == "journal-project"
    assert operation["project_root"] == str(root.resolve())

    operation["project_root"] = str((root / "moved").resolve())
    tampered_fingerprint = compile_memory._operation_replay_fingerprint(operation)
    journal["accepted_sha256"] = compile_memory._canonical_digest(journal["accepted"])
    compile_memory._write_journal(journal)

    replay = compile_memory.apply_compile_batch(request, response, False)

    assert first["ok"] is True
    assert original_fingerprint != tampered_fingerprint
    assert replay["ok"] is False
    assert replay["status"] == "journal_error"
    assert "project identity" in replay["error"]
    assert target.read_bytes() == before


def test_create_existing_slug_is_rejected_not_converted_to_update(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Existing targets must remain byte-for-byte unchanged."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-03.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "existing-target.md"
    _write_note(target, "Existing Target", "The original summary remains authoritative.")
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "existing-target", quote)),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "create target" in result["error"]
    assert target.read_bytes() == before


def test_create_rejects_lexically_existing_symlink_target_before_journal(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    quote = "A dangling lexical target is not an absent create destination."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-03-lexical.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    target = root / "knowledge" / "notes" / "lexical-target.md"
    real_lstat = Path.lstat

    class SymlinkMetadata:
        st_mode = stat.S_IFLNK | 0o777
        st_file_attributes = 0

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path, *args, **kwargs: SymlinkMetadata()
        if path == target
        else real_lstat(path, *args, **kwargs),
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "lexical-target", quote)),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "create target" in result["error"]


def test_update_missing_slug_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Updates require an already existing durable target."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-04.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    target = root / "knowledge" / "notes" / "missing-update.md"

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "missing-update",
                quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "update target" in result["error"]
    assert not target.exists()


def test_update_rejects_native_hard_link_alias_before_journal(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Updates must not split a hard-linked durable page from its alias."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-19-hard-link.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "hard-linked-update.md"
    alias = target.with_name("hard-linked-update-alias.md")
    _write_note(target, "Hard Linked Update", "Existing hard-linked page.")
    _hard_link_or_skip(target, alias)
    before_bytes = target.read_bytes()
    before_mode = stat.S_IMODE(target.stat().st_mode)
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "hard-linked" in result["error"]
    assert target.read_bytes() == before_bytes
    assert alias.read_bytes() == before_bytes
    assert stat.S_IMODE(target.stat().st_mode) == before_mode
    assert stat.S_IMODE(alias.stat().st_mode) == before_mode
    assert os.path.samefile(target, alias)
    assert compile_memory._read_knowledge_page_snapshot(target)[1]["nlink"] >= 2


def test_conditional_snapshot_revalidates_hard_link_count(compile_env):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "hard-link-cas.md"
    alias = target.with_name("hard-link-cas-alias.md")
    _write_note(target, "Hard Link CAS", "Existing single-link page.")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    assert expected["nlink"] == 1
    _hard_link_or_skip(target, alias)
    before = target.read_bytes()

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(OSError, match="changed"):
            memory_state.prepare_conditional_atomic_write(
                target,
                "replacement must not publish\n",
                expected,
                "hard-link-cas",
            )

    assert target.read_bytes() == before
    assert alias.read_bytes() == before


def test_create_matching_active_normalized_slug_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Normalized slugs identify the same durable subject."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-05.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "same_slug.md"
    _write_note(existing, "Unrelated Existing Title", "An unrelated existing summary.")
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "same-slug", quote)),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized slug" in result["error"]
    assert not (root / "knowledge" / "notes" / "same-slug.md").exists()


def test_create_matching_active_normalized_title_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Compatibility-equivalent titles are exact duplicate keys."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-06.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "title-source.md"
    _write_note(
        existing,
        "**Ｆail—Closed**   Admission",
        "An unrelated existing summary.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-title-slug",
        quote,
        title="fail closed admission",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert existing.read_bytes() == before
    assert not (root / "knowledge" / "notes" / "different-title-slug.md").exists()


def test_create_matching_linked_active_title_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Link destinations are not part of a durable title's visible text."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-06-linked.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "linked-title-source.md"
    _write_note(
        existing,
        "[Fail Closed Admission](https://example.test/policies/admission)",
        "An unrelated existing summary.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-linked-title-slug",
        quote,
        title="fail closed admission",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert existing.read_bytes() == before
    assert not (
        root / "knowledge" / "notes" / "different-linked-title-slug.md"
    ).exists()


def test_create_matching_aliased_wikilink_title_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Wikilink aliases, rather than destinations, are visible title text."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-06-wikilink.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "wikilink-title-source.md"
    _write_note(
        existing,
        "[[policy|Fail Closed Admission]]",
        "An unrelated existing summary.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-wikilink-title-slug",
        quote,
        title="Fail Closed Admission",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert existing.read_bytes() == before
    assert not (
        root / "knowledge" / "notes" / "different-wikilink-title-slug.md"
    ).exists()


def test_create_matching_literal_wikilink_code_title_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Code span markup remains literal visible title text."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-06-code-span.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "code-span-title-source.md"
    _write_note(
        existing,
        "`[[policy|Fail Closed Admission]]`",
        "An unrelated existing summary.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-code-span-title-slug",
        quote,
        title="policy Fail Closed Admission",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert existing.read_bytes() == before
    assert not (
        root / "knowledge" / "notes" / "different-code-span-title-slug.md"
    ).exists()


def test_create_matching_nested_markdown_title_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Nested destinations are not part of a durable title's visible text."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-06-nested-link.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "nested-title-source.md"
    _write_note(
        existing,
        "[![Fail Closed Admission](diagram.png)]"
        "(https://example.test/policies/admission)",
        "An unrelated existing summary.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-nested-title-slug",
        quote,
        title="fail closed admission",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert existing.read_bytes() == before
    assert not (
        root / "knowledge" / "notes" / "different-nested-title-slug.md"
    ).exists()


def test_create_matching_adjacent_reference_link_title_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Adjacent reference links retain both visible title labels."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-06-adjacent-reference.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "adjacent-reference-title.md"
    _write_note(
        existing,
        "[Fail ][first][Closed][second]",
        "An unrelated existing summary.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-adjacent-reference-slug",
        quote,
        title="Fail First Closed Second",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert existing.read_bytes() == before
    assert not (
        root / "knowledge" / "notes" / "different-adjacent-reference-slug.md"
    ).exists()


def test_block_scalar_status_text_does_not_hide_active_duplicate(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Indented block scalar text cannot change note lifecycle metadata."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-06-block-scalar.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "block-scalar-status.md"
    existing.write_text(
        "---\n"
        "type: pattern\n"
        "description: |\n"
        "  Historical lifecycle example:\n"
        "  status: archived\n"
        "status: active\n"
        "---\n\n"
        "# Block Scalar Active Title\n\n"
        "One-sentence summary: An unrelated existing summary.\n",
        encoding="utf-8",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-block-scalar-slug",
        quote,
        title="block scalar active title",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert existing.read_bytes() == before
    assert not (
        root / "knowledge" / "notes" / "different-block-scalar-slug.md"
    ).exists()


def test_create_matching_active_normalized_summary_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Decorated summaries cannot evade exact duplicate admission."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-07.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "summary-source.md"
    _write_note(
        existing,
        "An unrelated existing title",
        "**One durable rule:**  fail—closed!",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-summary-slug",
        quote,
        summary="one durable rule fail closed",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized summary" in result["error"]
    assert existing.read_bytes() == before
    assert not (root / "knowledge" / "notes" / "different-summary-slug.md").exists()


def test_create_matching_entity_encoded_active_summary_is_rejected(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Character references and their rendered text share a summary key."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-07-entity-summary.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "entity-summary-source.md"
    _write_note(
        existing,
        "An unrelated existing title",
        "Fail&nbsp;Closed admission policy.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "different-entity-summary-slug",
        quote,
        summary="Fail Closed admission policy",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized summary" in result["error"]
    assert existing.read_bytes() == before
    assert not (
        root / "knowledge" / "notes" / "different-entity-summary-slug.md"
    ).exists()


def test_create_plain_title_does_not_match_escaped_entity_literal(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Escaped entity text remains literal in active title keys."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-07-escaped-entity.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "escaped-entity-title.md"
    _write_note(
        existing,
        r"Fail\&nbsp;Closed",
        "An unrelated existing summary.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "plain-entity-title",
        quote,
        title="Fail Closed",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert existing.read_bytes() == before
    assert (root / "knowledge" / "notes" / "plain-entity-title.md").is_file()


def test_two_duplicate_creates_reject_whole_plan(compile_env):
    compile_memory, root, state_root, state = compile_env
    first_quote = "The first proposed note has valid evidence."
    second_quote = "The second proposed note also has valid evidence."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-08.md",
        [
            _project_block(
                "12:00:00",
                Path(_SYNTHETIC_PROJECT_ROOT),
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {first_quote}\n- {second_quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    first = _admission_operation(
        daily,
        "first-proposal",
        first_quote,
        title="Duplicate **Plan** Title",
        summary="First summary is distinct.",
    )
    second = _admission_operation(
        daily,
        "second-proposal",
        second_quote,
        title="duplicate plan title",
        summary="Second summary is distinct.",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(first, second),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "earlier create" in result["error"]
    assert not (root / "knowledge" / "notes" / "first-proposal.md").exists()
    assert not (root / "knowledge" / "notes" / "second-proposal.md").exists()


def test_duplicate_outside_prompt_snapshot_is_rejected_from_live_corpus(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Admission must inspect the corpus at apply time."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-09.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert "Late Live Corpus Title" not in request["prompt"]
    late_page = root / "knowledge" / "notes" / "late-live-page.md"
    _write_note(late_page, "Late Live Corpus Title", "A late live summary.")
    before = late_page.read_bytes()
    operation = _admission_operation(
        daily,
        "late-proposal",
        quote,
        title="late live corpus title",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "normalized title" in result["error"]
    assert late_page.read_bytes() == before
    assert not (root / "knowledge" / "notes" / "late-proposal.md").exists()


def test_provider_audit_inflation_cannot_bypass_and_values_are_python_derived(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    status_quote = "Status only: implementation finished and tests passed."
    lesson_quote = "Validate durable source sections before creating the journal."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-10.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"{status_quote}\n\n**Lessons / patterns**\n- {lesson_quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    inflated = {
        "verified": 1_000_000,
        "dedup": 1_000_000,
        "stubs": 0,
        "contradictions": 0,
        "rejected": 0,
    }

    rejected = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(daily, "inflated-status", status_quote),
            audit=inflated,
        ),
        False,
    )

    _assert_rejected_before_journal(rejected, request, daily, state_root, state)
    accepted = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(daily, "derived-audit", lesson_quote),
            audit=inflated,
        ),
        False,
    )
    audit = compile_memory.parse_compile_audit(accepted["audit"])
    assert accepted["ok"] is True
    assert audit["verified"] == 1
    assert audit["dedup"] == 3


def test_create_body_word_bounds_are_enforced_inclusively(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Validate deterministic inclusive body bounds before publication."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11.md",
        [_generated_block("12:00:00", root, f"**Lessons / patterns**\n- {quote}")],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    for count, accepted in ((149, False), (150, True), (400, True), (401, False)):
        operation = _admission_operation(
            daily,
            f"body-{count}",
            quote,
            body=_word_body(count),
        )
        plan, error = compile_memory._normalize_accepted_plan(
            _response(operation, audit={"verified": 900, "dedup": 800}),
            [daily],
            source_blocks,
        )
        if accepted:
            assert error == ""
            assert plan is not None
            assert plan["operations"][0]["body_markdown"] == _word_body(count)
            assert plan["audit"]["verified"] == 1
            assert plan["audit"]["dedup"] == 3
        else:
            assert plan is None
            assert "150-400 words" in error


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("<i></i>" * 75, 0),
        (
            '<strong class="ignored" data-label="hidden words">'
            "real words</strong><!-- hidden words -->",
            2,
        ),
    ),
)
def test_body_word_count_uses_visible_html_text(compile_env, body, expected):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("<i></i><!-- " + _word_body(500) + " -->", 0),
        ("`<!-- " + _word_body(500) + " -->`", 500),
        ("````\n<!-- " + _word_body(500) + " -->\n````", 500),
        (" ".join(["&nbsp;"] * 150), 0),
        (" ".join(["&#32;"] * 150), 0),
        ("`&nbsp; &#32;`", 2),
    ),
)
def test_body_word_count_shields_code_and_decodes_entities(
    compile_env,
    body,
    expected,
):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == expected


def test_body_word_count_removes_link_reference_definition_blocks(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    definitions = "\n".join(
        f'[hidden-{index}]: /destination-{index}\n  "invisible title {index}"'
        for index in range(50)
    )

    assert compile_memory._body_word_count(definitions) == 0


@pytest.mark.parametrize("indent", ("    ", "\t"))
def test_body_word_count_shields_standalone_indented_code(compile_env, indent):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        indent
        + "<!-- "
        + _word_body(75)
        + " -->\n\n"
        + indent
        + "<!-- "
        + _word_body(75)
        + " -->"
    )

    assert compile_memory._body_word_count(body) == 150


@pytest.mark.parametrize("tag", ("script", "style"))
def test_body_word_count_removes_raw_script_and_style_blocks(compile_env, tag):
    compile_memory, _root, _state_root, _state = compile_env
    body = f'<{tag} data-label="not visible">{_word_body(150)}</{tag.upper()}>'

    assert compile_memory._body_word_count(body) == 0
    assert compile_memory._body_word_count(f"<{tag}>{_word_body(150)}") == 0
    assert compile_memory._body_word_count(f"`{body}`") == 156


@pytest.mark.parametrize(
    ("name", "daily_suffix", "closed", "unclosed", "code_expected"),
    (
        (
            "processing-instruction",
            "pi",
            "<?\n{words}\n?>",
            "<?\n{words}",
            150,
        ),
        (
            "declaration",
            "decl",
            "<!A\n{words}\n>",
            "<!A\n{words}",
            151,
        ),
        (
            "cdata",
            "cdata",
            "<![CDATA[\n{words}\n]]>",
            "<![CDATA[\n{words}",
            151,
        ),
    ),
)
def test_commonmark_html_types_3_to_5_are_hidden_in_full_admission(
    compile_env,
    name,
    daily_suffix,
    closed,
    unclosed,
    code_expected,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = f"CommonMark {name} visibility is deterministic."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-09-12-{daily_suffix}.md",
        [_block("12:00:00", quote)],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    words = _word_body(150)

    for suffix, template in (("closed", closed), ("unclosed", unclosed)):
        body = template.format(words=words)
        plan, error = compile_memory._normalize_accepted_plan(
            _response(
                _admission_operation(
                    daily,
                    f"html-{name}-{suffix}",
                    quote,
                    body=body,
                )
            ),
            [daily],
            source_blocks,
        )
        assert compile_memory._body_word_count(body) == 0
        assert plan is None
        assert "150-400 words" in error

    code_body = f"`{closed.format(words=words)}`"
    plan, error = compile_memory._normalize_accepted_plan(
        _response(
            _admission_operation(
                daily,
                f"html-{name}-code",
                quote,
                body=code_body,
            )
        ),
        [daily],
        source_blocks,
    )
    assert compile_memory._body_word_count(code_body) == code_expected
    assert error == ""
    assert plan is not None


@pytest.mark.parametrize(
    "body",
    (
        "<?" + _word_body(150) + "?>",
        "<!A " + _word_body(150) + ">",
        "<![CDATA[" + _word_body(150) + "]]>",
    ),
)
def test_commonmark_html_type_3_to_5_closers_hide_the_complete_construct(
    compile_env,
    body,
):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == 0


@pytest.mark.parametrize(
    "opener",
    ("<script>", "<!--", "<?", "<!A", "<![CDATA["),
    ids=("type-1", "type-2", "type-3", "type-4", "type-5"),
)
def test_commonmark_html_types_1_to_5_stop_at_sibling_list_item_identity(
    compile_env,
    opener,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = f"- {opener}\n- {_word_body(150)}"

    assert compile_memory._body_word_count(body) == 150


@pytest.mark.parametrize(
    "opener",
    ("<script>", "<!--", "<?", "<!A", "<![CDATA["),
    ids=("type-1", "type-2", "type-3", "type-4", "type-5"),
)
def test_commonmark_html_types_1_to_5_stop_at_nested_sibling_identity(
    compile_env,
    opener,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = f"- outer\n  - {opener}\n  - {_word_body(150)}"

    assert compile_memory._body_word_count(body) == 151


@pytest.mark.parametrize(
    "opener",
    ("<script>", "<!--", "<?", "<!A", "<![CDATA["),
    ids=("type-1", "type-2", "type-3", "type-4", "type-5"),
)
def test_commonmark_html_types_1_to_5_keep_same_list_item_continuation_raw(
    compile_env,
    opener,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = f"- {opener}\n  {_word_body(150)}"

    assert compile_memory._body_word_count(body) == 0


@pytest.mark.parametrize(
    "opener",
    ("<script>", "<!--", "<?", "<!A", "<![CDATA["),
    ids=("type-1", "type-2", "type-3", "type-4", "type-5"),
)
def test_commonmark_html_type_sibling_words_satisfy_full_create_admission(
    compile_env,
    opener,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Container-scoped HTML leaves sibling evidence visible."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-12-html-container.md",
        [_block("12:00:00", quote)],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    body = f"- {opener}\n- {_word_body(150)}"

    plan, error = compile_memory._normalize_accepted_plan(
        _response(
            _admission_operation(
                daily,
                f"html-container-{ord(opener[1]):x}-{len(opener)}",
                quote,
                body=body,
            )
        ),
        [daily],
        source_blocks,
    )

    assert error == ""
    assert plan is not None


def test_commonmark_html_container_scan_has_linear_line_bound(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    openers = ("<script>", "<!--", "<?", "<!A", "<![CDATA[")

    def project(items):
        body = "\n".join(
            f"- {openers[index % len(openers)]}\n- visible-{index}"
            for index in range(items)
        )
        scans = {}
        compile_memory._markdown_visible_text(body, scan_stats=scans)
        assert scans["html_block_lines"] <= len(body.splitlines()) * 2
        return scans["html_block_lines"]

    small = project(200)
    large = project(400)

    assert large <= small * 2 + 2


@pytest.mark.parametrize("tag", ("script", "style"))
def test_self_closing_raw_block_opener_hides_through_matching_close(
    compile_env,
    tag,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = f"<{tag}/>{_word_body(150)}</{tag}>"

    assert compile_memory._body_word_count(body) == 0
    assert compile_memory._body_word_count(f"`{body}`") == 152


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("<div>\n</div>\n    <!-- " + _word_body(150) + " -->", 0),
        ("<span>paragraph</span>\n    <!-- " + _word_body(150) + " -->", 1),
        ("<div>\n</div>\n\n    <!-- " + _word_body(150) + " -->", 150),
        ("<div>\nordinary rendered text\n</div>", 3),
        ("<span>\n    <!-- " + _word_body(150) + " -->", 0),
        ("paragraph\n<span>\n    <!-- " + _word_body(150) + " -->", 1),
    ),
)
def test_type_6_and_7_html_blocks_end_only_at_blank_line(
    compile_env,
    body,
    expected,
):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == expected


@pytest.mark.parametrize("tag", ("div", "custom-tag"))
@pytest.mark.parametrize("prefix", ("", "> "))
def test_blank_terminated_html_does_not_cross_sibling_list_item_identity(
    compile_env,
    tag,
    prefix,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        f"{prefix}- <{tag}>\n"
        f"{prefix}-     <!-- {_word_body(150)} -->"
    )

    assert compile_memory._body_word_count(body) == 150


@pytest.mark.parametrize("tag", ("div", "custom-tag"))
def test_blank_terminated_html_keeps_same_list_item_continuation_raw(
    compile_env,
    tag,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = f"- <{tag}>\n      <!-- {_word_body(150)} -->"

    assert compile_memory._body_word_count(body) == 0


@pytest.mark.parametrize(
    "definition",
    (
        '> [hidden]: /destination\n>   "invisible title"',
        '- [hidden]: /destination\n  "invisible title"',
        '> - [hidden]: /destination\n>   "invisible title"',
    ),
)
def test_link_reference_definitions_are_hidden_inside_containers(
    compile_env,
    definition,
):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(definition) == 0
    assert compile_memory._body_word_count(
        definition + "\n\n[visible][hidden]\n`> [code]: /literal`"
    ) == 3


def test_reference_definition_scan_has_deterministic_linear_line_bound(compile_env):
    compile_memory, _root, _state_root, _state = compile_env

    def project(definitions):
        body = "\n".join(
            f'> [hidden-{index}]: /destination-{index}\n> "title {index}"'
            for index in range(definitions)
        )
        scans = {}
        assert compile_memory._body_word_count(body) == 0
        compile_memory._markdown_visible_text(body, scan_stats=scans)
        assert scans["reference_lines"] <= len(body.splitlines()) * 2
        return scans["reference_lines"]

    small = project(200)
    large = project(400)

    assert large <= small * 2 + 2


def test_nested_collapsed_reference_projection_has_linear_character_bound(
    compile_env,
):
    compile_memory, _root, _state_root, _state = compile_env
    depth = 15_000
    adversarial = "[" * depth + "visible" + "][]" * depth
    body = (
        "[visible]: /resolved\n\n"
        + adversarial
        + " [shown][missing] ![alt][visible]"
    )
    scans = {}

    visible = compile_memory._markdown_visible_text(body, scan_stats=scans)

    assert len(body) >= 60_000
    assert compile_memory._body_word_count(body) == 4
    assert "shown" in visible and "missing" in visible
    assert "alt" in visible and "visible" in visible
    assert scans.get("reference_label_characters", len(body) * 100) <= len(body) * 4


def test_reference_label_over_commonmark_limit_remains_visible(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    oversized = "x" * 1_000
    body = f"[{oversized}]: /ignored\n\n[shown][{oversized}]"

    assert compile_memory._body_word_count(body) == 4


@pytest.mark.parametrize(
    "body",
    (
        '- [hidden]: /destination\n  "unterminated\n- sibling visible words"',
        '> - [hidden]: /destination\n>   "unterminated\n> - sibling visible words"',
        '- outer\n  - [hidden]: /destination\n    "unterminated\n  - sibling visible words"',
    ),
)
def test_unterminated_reference_title_cannot_consume_sibling_list_item(
    compile_env,
    body,
):
    compile_memory, _root, _state_root, _state = compile_env

    assert "sibling visible words" in compile_memory._markdown_visible_text(body)


def test_reference_title_can_continue_within_same_list_item(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = '- [hidden]: /destination\n  "continued\n  title"'

    assert compile_memory._body_word_count(body) == 0


@pytest.mark.parametrize(
    ("prefix", "expected"),
    (
        ("# Heading\n", 151),
        ("Heading\n===\n", 151),
        ("***\n", 150),
        ("<div>\n</div>\n", 0),
        ("```\nleaf\n```\n", 151),
    ),
)
def test_indented_code_starts_immediately_after_nonparagraph_leaf_blocks(
    compile_env,
    prefix,
    expected,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = prefix + "    <!-- " + _word_body(150) + " -->"

    assert compile_memory._body_word_count(body) == expected


def test_indented_code_cannot_interrupt_paragraph_and_list_continuation_wins(
    compile_env,
):
    compile_memory, _root, _state_root, _state = compile_env
    hidden = "<!-- " + _word_body(150) + " -->"

    assert compile_memory._body_word_count("Paragraph\n    " + hidden) == 1
    assert compile_memory._body_word_count("- item\n    " + hidden) == 1


@pytest.mark.parametrize(
    "body",
    (
        pytest.param(
            "> ```\n> <!-- " + _word_body(500) + " -->\n> ```",
            id="blockquote-backtick",
        ),
        pytest.param(
            "- ~~~\n  <!-- " + _word_body(500) + " -->\n  ~~~",
            id="unordered-list-tilde",
        ),
        pytest.param(
            "1. ```\n   <!-- " + _word_body(500) + " -->\n   ```",
            id="ordered-list-backtick",
        ),
        pytest.param(
            "> - ~~~\n>   <!-- " + _word_body(500) + " -->\n>   ~~~",
            id="blockquote-list-tilde",
        ),
    ),
)
def test_body_word_count_shields_container_prefixed_fences(compile_env, body):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == 500


@pytest.mark.parametrize("fence", ("~~~", "```"))
def test_body_word_count_retains_list_continuation_across_blank_line(
    compile_env,
    fence,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(399)
        + "\n\n   - item\n\n"
        + f"     {fence}\n"
        + "     <!-- "
        + _word_body(100)
        + " -->\n"
        + f"     {fence}"
    )

    assert compile_memory._body_word_count(body) == 500


def test_body_word_count_retains_ordered_list_continuation(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(399)
        + "\n\n   1. item\n\n"
        + "      ~~~\n"
        + "      <!-- "
        + _word_body(100)
        + " -->\n"
        + "      ~~~"
    )

    assert compile_memory._body_word_count(body) == 501


def test_body_word_count_retains_nested_quote_list_continuation(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(399)
        + "\n\n>   - item\n>\n"
        + ">     ```\n"
        + ">     <!-- "
        + _word_body(100)
        + " -->\n"
        + ">     ```"
    )

    assert compile_memory._body_word_count(body) == 500


@pytest.mark.parametrize("fence", ("~~~", "```"))
def test_body_word_count_appends_nested_list_continuation(
    compile_env,
    fence,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(398)
        + "\n\n- outer\n"
        + "     - inner\n\n"
        + f"       {fence}\n"
        + "       <!-- "
        + _word_body(100)
        + " -->\n"
        + f"       {fence}"
    )

    assert compile_memory._body_word_count(body) == 500


@pytest.mark.parametrize(
    ("prior_words", "list_line", "fence_prefix", "fence"),
    (
        pytest.param(399, "-\titem", "\t", "~~~", id="unordered-tilde"),
        pytest.param(398, "1.\titem", "\t", "```", id="ordered-backtick"),
        pytest.param(399, " -\titem", "\t", "~~~", id="column-two"),
        pytest.param(399, "  -\titem", "\t", "```", id="column-three"),
        pytest.param(399, "   -\titem", "\t\t", "~~~", id="column-four"),
    ),
)
def test_body_word_count_expands_tabs_for_list_continuation_fences(
    compile_env,
    prior_words,
    list_line,
    fence_prefix,
    fence,
):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(prior_words)
        + "\n\n"
        + list_line
        + "\n\n"
        + fence_prefix
        + fence
        + "\n"
        + fence_prefix
        + "<!-- "
        + _word_body(100)
        + " -->\n"
        + fence_prefix
        + fence
    )

    assert compile_memory._body_word_count(body) == 500


def test_body_word_count_expands_tabs_after_quote_and_list_markers(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(399)
        + "\n\n>\t-\titem\n>\n"
        + ">\t\t~~~\n"
        + ">\t\t<!-- "
        + _word_body(100)
        + " -->\n"
        + ">\t\t~~~"
    )

    assert compile_memory._body_word_count(body) == 500


def test_body_word_count_expands_mixed_tab_closing_fence_prefix(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(399)
        + "\n\n-\titem\n\n"
        + "\t~~~\n"
        + "\t<!-- "
        + _word_body(100)
        + " -->\n"
        + " \t~~~"
    )

    assert compile_memory._body_word_count(body) == 500


@pytest.mark.parametrize(
    "body",
    (
        "alpha\tbeta",
        "`alpha\tbeta`",
        "```\nalpha\tbeta\n```",
    ),
)
def test_body_word_count_treats_visible_tabs_as_word_separators(compile_env, body):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == 2


@pytest.mark.parametrize(
    "body",
    (
        pytest.param(
            _word_body(397)
            + "\n\n1. outer\n"
            + "      - inner\n\n"
            + "        ```\n"
            + "        <!-- "
            + _word_body(100)
            + " -->\n"
            + "        ```",
            id="ordered-unordered-backtick",
        ),
        pytest.param(
            _word_body(397)
            + "\n\n- outer\n"
            + "     1. inner\n\n"
            + "        ~~~\n"
            + "        <!-- "
            + _word_body(100)
            + " -->\n"
            + "        ~~~",
            id="unordered-ordered-tilde",
        ),
    ),
)
def test_body_word_count_appends_mixed_nested_list_containers(
    compile_env,
    body,
):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == 500


def test_body_word_count_appends_nested_list_after_quote_containers(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(398)
        + "\n\n> - outer\n"
        + ">      - inner\n>\n"
        + ">        ~~~\n"
        + ">        <!-- "
        + _word_body(100)
        + " -->\n"
        + ">        ~~~"
    )

    assert compile_memory._body_word_count(body) == 500


def test_nested_list_sibling_replaces_active_inner_container(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        _word_body(397)
        + "\n\n- outer\n"
        + "     - first\n"
        + "     - sibling\n\n"
        + "       ~~~\n"
        + "       <!-- "
        + _word_body(100)
        + " -->\n"
        + "       ~~~"
    )

    assert compile_memory._body_word_count(body) == 500


def test_nested_list_dedent_pops_active_inner_container(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        "- outer\n"
        "     - inner\n"
        "  outside\n"
        "       ~~~\n"
        "       <!-- "
        + _word_body(100)
        + " -->\n"
        "       ~~~"
    )

    assert compile_memory._body_word_count(body) == 3


def test_dedented_line_ends_retained_list_continuation(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        "   - item\n\n"
        "outside\n"
        "     ~~~\n"
        "     <!-- "
        + _word_body(100)
        + " -->\n"
        "     ~~~"
    )

    assert compile_memory._body_word_count(body) == 2


def test_fenced_code_closer_cannot_cross_container_segment(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        "> - ~~~\n"
        ">   <!-- "
        + _word_body(100)
        + " -->\n"
        "~~~\n"
        "<!-- "
        + _word_body(500)
        + " -->"
    )

    assert compile_memory._body_word_count(body) == 600


@pytest.mark.parametrize("fence", ("```", "~~~"))
def test_inline_code_matching_resets_at_fenced_code(compile_env, fence):
    compile_memory, _root, _state_root, _state = compile_env
    body = (
        "`"
        + _word_body(400)
        + f"\n{fence}\n"
        + _word_body(100)
        + f"\n{fence}\n`"
    )

    assert compile_memory._body_word_count(body) == 500


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        pytest.param(
            "\\<!-- " + _word_body(500) + " -->",
            500,
            id="escaped-comment",
        ),
        pytest.param(
            "<!-- " + _word_body(500) + " -->",
            0,
            id="comment",
        ),
        pytest.param(
            "\\\\<!-- " + _word_body(500) + " -->",
            0,
            id="even-backslashes-comment",
        ),
        pytest.param(r"\<i>visible</i>", 2, id="escaped-tag"),
    ),
)
def test_body_word_count_respects_escaped_html_openers(
    compile_env,
    body,
    expected,
):
    compile_memory, _root, _state_root, _state = compile_env

    assert compile_memory._body_word_count(body) == expected


def test_create_body_cannot_reach_minimum_with_empty_html_tags(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "HTML tag names and attributes are not visible durable body words."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-empty-html.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "empty-html-body",
        quote,
        body="<i></i>" * 75,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 0)" in result["error"]
    assert not (root / "knowledge" / "notes" / "empty-html-body.md").exists()


@pytest.mark.parametrize("entity", ("&nbsp;", "&#32;"))
def test_create_body_cannot_use_character_references_as_words(
    compile_env,
    entity,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Whitespace character references cannot pad a durable body."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-entity-padding.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "entity-padded-body",
        quote,
        body=" ".join([entity] * 150),
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 0)" in result["error"]
    assert not (root / "knowledge" / "notes" / "entity-padded-body.md").exists()


@pytest.mark.parametrize("entity", ("&nbsp", "&#32"))
def test_create_body_keeps_semicolonless_entity_text_visible(
    compile_env,
    entity,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Semicolonless entity-like text remains visible durable body text."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-semicolonless-entity.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "semicolonless-entity-body",
        quote,
        body=" ".join([entity] * 150),
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert (
        root / "knowledge" / "notes" / "semicolonless-entity-body.md"
    ).is_file()


def test_create_body_counts_escaped_comment_overflow_as_visible(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "An escaped comment opener leaves its body visible."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-escaped-comment.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "escaped-comment-overflow",
        quote,
        body="\\<!-- " + _word_body(500) + " -->",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 500)" in result["error"]
    assert not (
        root / "knowledge" / "notes" / "escaped-comment-overflow.md"
    ).exists()


@pytest.mark.parametrize(
    ("comment", "slug"),
    (("<!-->", "bogus-comment-short"), ("<!--->", "bogus-comment-dash")),
)
def test_create_body_counts_words_after_bogus_empty_comment(
    compile_env,
    comment,
    slug,
):
    compile_memory, root, state_root, state = compile_env
    quote = "A bogus empty comment cannot hide visible overflow words."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-20-{slug}.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    body = f"{_word_body(150)}\n{comment}\n{_word_body(300)}"
    operation = _admission_operation(daily, slug, quote, body=body)

    assert compile_memory._body_word_count(body) == 450
    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 450)" in result["error"]
    assert not (root / "knowledge" / "notes" / f"{slug}.md").exists()


def test_create_body_rejects_words_lost_across_fenced_code_boundary(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Malformed inline runs cannot span a fenced code block."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-fence-boundary.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    body = (
        "`"
        + _word_body(400)
        + "\n~~~\n"
        + _word_body(100)
        + "\n~~~\n`"
    )
    operation = _admission_operation(
        daily,
        "fence-boundary-overflow",
        quote,
        body=body,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 500)" in result["error"]
    assert not (
        root / "knowledge" / "notes" / "fence-boundary-overflow.md"
    ).exists()


def test_create_body_rejects_list_continuation_fence_overflow(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "A blank line does not discard active list continuation indentation."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-list-continuation.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    body = (
        _word_body(399)
        + "\n\n   - item\n\n"
        + "     ~~~\n"
        + "     <!-- "
        + _word_body(100)
        + " -->\n"
        + "     ~~~"
    )
    operation = _admission_operation(
        daily,
        "list-continuation-overflow",
        quote,
        body=body,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 500)" in result["error"]
    assert not (
        root / "knowledge" / "notes" / "list-continuation-overflow.md"
    ).exists()


def test_create_body_rejects_nested_list_continuation_overflow(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Nested list markers extend active continuation indentation."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-nested-list.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    body = (
        _word_body(398)
        + "\n\n- outer\n"
        + "     - inner\n\n"
        + "       ~~~\n"
        + "       <!-- "
        + _word_body(100)
        + " -->\n"
        + "       ~~~"
    )
    operation = _admission_operation(
        daily,
        "nested-list-overflow",
        quote,
        body=body,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 500)" in result["error"]
    assert not (
        root / "knowledge" / "notes" / "nested-list-overflow.md"
    ).exists()


def test_create_body_rejects_tab_stop_list_continuation_overflow(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Tabs advance list continuation indentation to four-column stops."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-tab-stop-list.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    body = (
        _word_body(399)
        + "\n\n-\titem\n\n"
        + "\t~~~\n"
        + "\t<!-- "
        + _word_body(100)
        + " -->\n"
        + "\t~~~"
    )
    operation = _admission_operation(
        daily,
        "tab-stop-list-overflow",
        quote,
        body=body,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 500)" in result["error"]
    assert not (
        root / "knowledge" / "notes" / "tab-stop-list-overflow.md"
    ).exists()


def test_create_body_counts_comment_like_fenced_code_as_visible(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Comment-like text in a code fence remains visible code."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-code-overflow.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "code-overflow-body",
        quote,
        body="````\n<!-- " + _word_body(500) + " -->\n````",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words for create (got 500)" in result["error"]
    assert not (root / "knowledge" / "notes" / "code-overflow-body.md").exists()


@pytest.mark.parametrize(
    ("count", "body"),
    (
        (150, "`<!-- " + _word_body(150) + " -->`"),
        (400, "````\n<!-- " + _word_body(400) + " -->\n````"),
    ),
)
def test_create_body_accepts_visible_code_at_exact_bounds(
    compile_env,
    count,
    body,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Literal code words count at inclusive durable body bounds."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-09-11-code-{count}.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        f"code-body-{count}",
        quote,
        body=body,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert (root / "knowledge" / "notes" / f"code-body-{count}.md").is_file()


@pytest.mark.parametrize(
    ("slug", "body", "accepted", "word_count"),
    (
        pytest.param(
            "reference-definition-padding",
            _word_body(149)
            + "\n\n"
            + "\n".join(
                f'[hidden-{index}]: /destination-{index} "invisible title"'
                for index in range(50)
            ),
            False,
            149,
            id="fifty-invisible-reference-definitions",
        ),
        pytest.param(
            "indented-code-minimum",
            "    <!-- " + _word_body(150) + " -->",
            True,
            150,
            id="indented-code-150",
        ),
        pytest.param(
            "indented-code-overflow",
            "    <!-- " + _word_body(500) + " -->",
            False,
            500,
            id="indented-code-500",
        ),
        pytest.param(
            "raw-script-padding",
            "<script>" + _word_body(150) + "</script>",
            False,
            0,
            id="script-150",
        ),
        pytest.param(
            "raw-style-padding",
            "<style>" + _word_body(150) + "</style>",
            False,
            0,
            id="style-150",
        ),
        pytest.param(
            "raw-self-closing-script-padding",
            "<script/>" + _word_body(150) + "</script>",
            False,
            0,
            id="script-self-closing-150",
        ),
        pytest.param(
            "raw-self-closing-style-padding",
            "<style/>" + _word_body(150) + "</style>",
            False,
            0,
            id="style-self-closing-150",
        ),
    ),
)
def test_create_body_applies_commonmark_block_visibility(
    compile_env,
    slug,
    body,
    accepted,
    word_count,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Block visibility must match rendered CommonMark semantics."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-blocks.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    target = root / "knowledge" / "notes" / f"{slug}.md"

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, slug, quote, body=body)),
        False,
    )

    if accepted:
        assert result["ok"] is True
        assert target.is_file()
    else:
        _assert_rejected_before_journal(result, request, daily, state_root, state)
        assert f"150-400 words for create (got {word_count})" in result["error"]
        assert not target.exists()


def test_rendered_single_line_metadata_rejects_line_breaks(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Rendered metadata must retain the values admitted for publication."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-metadata-lines.md",
        [_block("12:00:00", quote)],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    for field in ("title", "summary", "body_section", "claim", "related"):
        operation = _admission_operation(
            daily,
            f"metadata-{field}",
            quote,
        )
        if field == "claim":
            operation["evidence"][0]["claim"] = "claim\n## injected"
        elif field == "related":
            operation["related"] = ["[[safe]]\r\n## injected"]
        else:
            operation[field] = f"safe\n## injected {field}"

        plan, error = compile_memory._normalize_accepted_plan(
            _response(operation),
            [daily],
            source_blocks,
        )

        assert plan is None
        assert "line breaks" in error


@pytest.mark.parametrize(
    ("body", "accepted"),
    (
        (_word_body(149) + "\n\n<!-- hiddenpadding -->", False),
        (_word_body(148) + "\n\n[visible](hiddenpadding)", False),
        (_word_body(400) + "\n\n<!-- " + _word_body(100) + " -->", True),
    ),
)
def test_create_body_word_bounds_count_only_visible_markdown(
    compile_env,
    body,
    accepted,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Hidden Markdown cannot pad or overflow the durable body threshold."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-visible-body.md",
        [_block("12:00:00", quote)],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    operation = _admission_operation(
        daily,
        "visible-body-count",
        quote,
        body=body,
    )

    plan, error = compile_memory._normalize_accepted_plan(
        _response(operation),
        [daily],
        source_blocks,
    )

    assert (plan is not None) is accepted
    assert (error == "") is accepted


@pytest.mark.parametrize(
    "resource",
    (
        "response",
        "operations",
        "evidence",
        "related",
        "title",
        "body",
        "quote",
        "claim",
        "related-item",
    ),
)
def test_provider_plan_enforces_explicit_resource_limits(
    compile_env,
    monkeypatch,
    resource,
):
    compile_memory, root, _state_root, _state = compile_env
    daily = root / "knowledge" / "daily" / "2026-09-11-provider-limits.md"
    operation = _operation(daily, "provider-limits", "LIMIT_BODY")
    raw = _response(operation)
    if resource == "response":
        monkeypatch.setattr(
            compile_memory,
            "MAX_PROVIDER_RESPONSE_CHARS",
            len(raw) - 1,
            raising=False,
        )
    elif resource == "operations":
        monkeypatch.setattr(
            compile_memory, "MAX_PROVIDER_OPERATIONS", 1, raising=False
        )
        raw = _response(operation, dict(operation, slug="provider-limits-two"))
    elif resource == "evidence":
        monkeypatch.setattr(
            compile_memory, "MAX_PROVIDER_EVIDENCE", 1, raising=False
        )
        operation["evidence"].append(dict(operation["evidence"][0]))
        raw = _response(operation)
    elif resource == "related":
        monkeypatch.setattr(
            compile_memory, "MAX_PROVIDER_RELATED", 1, raising=False
        )
        operation["related"] = ["[[one]]", "[[two]]"]
        raw = _response(operation)
    elif resource == "title":
        monkeypatch.setattr(
            compile_memory, "MAX_PROVIDER_METADATA_CHARS", 4, raising=False
        )
    elif resource == "body":
        monkeypatch.setattr(
            compile_memory, "MAX_PROVIDER_BODY_CHARS", 10, raising=False
        )
    elif resource == "quote":
        monkeypatch.setattr(
            compile_memory,
            "MAX_PROVIDER_EVIDENCE_QUOTE_CHARS",
            4,
            raising=False,
        )
    elif resource == "claim":
        monkeypatch.setattr(
            compile_memory, "MAX_PROVIDER_CLAIM_CHARS", 4, raising=False
        )
    else:
        monkeypatch.setattr(
            compile_memory,
            "MAX_PROVIDER_RELATED_ITEM_CHARS",
            4,
            raising=False,
        )
        operation["related"] = ["[[long-related-item]]"]
        raw = _response(operation)

    plan, error = compile_memory._parse_provider_plan(raw)

    assert plan is None
    assert "limit" in error


def test_plan_admission_reads_each_daily_once_for_all_operations(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = "One source snapshot supports the first operation in an accepted plan."
    second_quote = "The same snapshot also supports a distinct second operation."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-single-read.md",
        [_block("12:00:00", quote), _block("12:01:00", second_quote)],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    first = _admission_operation(
        daily,
        "single-read-one",
        quote,
        summary="First distinct summary.",
    )
    second = _admission_operation(
        daily,
        "single-read-two",
        second_quote,
        summary="Second distinct summary.",
        timestamp="12:01:00",
    )
    real_read_snapshot = compile_memory._read_daily_snapshot
    daily_reads = 0

    def counting_read_snapshot(path):
        nonlocal daily_reads
        if path == daily:
            daily_reads += 1
        return real_read_snapshot(path)

    monkeypatch.setattr(
        compile_memory,
        "_read_daily_snapshot",
        counting_read_snapshot,
    )

    plan, error = compile_memory._normalize_accepted_plan(
        _response(first, second),
        [daily],
        source_blocks,
    )

    assert error == ""
    assert plan is not None
    assert daily_reads == 1


def test_manifest_preparation_reads_daily_a_constant_number_of_times(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-22-cached-source.md",
        [
            _block(
                f"12:{index // 60:02d}:{index % 60:02d}",
                f"Durable cached source evidence {index}. " + "x" * 80,
            )
            for index in range(100)
        ],
    )
    real_read_snapshot = compile_memory._read_daily_snapshot
    daily_reads = 0

    def counting_read_snapshot(path):
        nonlocal daily_reads
        if Path(path) == daily:
            daily_reads += 1
        return real_read_snapshot(path)

    monkeypatch.setattr(
        compile_memory,
        "_read_daily_snapshot",
        counting_read_snapshot,
    )

    request = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=6_000,
    )

    assert request["pending"] is True
    assert request["dailies"][0]["sha256"] == compile_memory._daily_snapshot_hash(
        daily
    )
    assert daily_reads <= 3


def test_plan_admission_rejects_quote_occurrence_overflow(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Always reject repeated exact evidence over the occurrence limit."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-11-occurrence-limit.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Lessons / patterns**\n" + "\n".join([f"- {quote}"] * 3),
            )
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        compile_memory,
        "MAX_EVIDENCE_QUOTE_OCCURRENCES",
        2,
        raising=False,
    )

    plan, error = compile_memory._normalize_accepted_plan(
        _response(_admission_operation(daily, "occurrence-limit", quote)),
        [daily],
        source_blocks,
    )

    assert plan is None
    assert "occurrence limit" in error


def test_execute_plan_builds_one_bounded_contradiction_snapshot(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, _state = compile_env
    _write_note(
        root / "knowledge" / "notes" / "existing-auth-rule.md",
        "Existing Auth Rule",
        "Existing auth summary.",
    )
    real_inventory = compile_memory.bounded_path_inventory
    inventory_calls = 0

    def counting_inventory(*args, **kwargs):
        nonlocal inventory_calls
        inventory_calls += 1
        return real_inventory(*args, **kwargs)

    monkeypatch.setattr(compile_memory, "bounded_path_inventory", counting_inventory)
    monkeypatch.setattr(compile_memory, "_verify_evidence", lambda *_args: (1, 0))
    operations = []
    for suffix in ("one", "two"):
        operations.append(
            {
                "action": "create",
                "category": "patterns",
                "slug": f"new-auth-rule-{suffix}",
                "title": f"New Auth Rule {suffix}",
                "summary": f"Distinct {suffix} summary.",
                "body_section": "Lesson",
                "body_markdown": "Sessions are no longer used.",
                "evidence": [{"daily_date": "x", "timestamp": "x", "claim": "x"}],
                "related": [],
            }
        )

    compile_memory._execute_plan(
        {"operations": operations, "audit": {}},
        [],
        True,
    )

    assert inventory_calls == 1


@pytest.mark.parametrize("status", ("archived", "superseded"))
def test_inactive_note_allows_different_slug_reuse_but_blocks_exact_target(
    compile_env,
    status,
):
    compile_memory, root, state_root, state = compile_env
    inactive = root / "knowledge" / "notes" / f"inactive-{status}.md"
    title = f"Reusable Inactive {status.title()} Title"
    summary = f"Reusable inactive {status} summary."
    _write_note(inactive, title, summary, status=status)
    original = inactive.read_bytes()
    reuse_quote = "Inactive metadata does not reserve title and summary keys."
    reuse_daily = _daily(
        root / "knowledge" / "daily" / f"2026-09-1{2 if status == 'archived' else 3}.md",
        [_block("12:00:00", reuse_quote)],
    )
    reuse_request = compile_memory.prepare_compile_request(
        [reuse_daily], state, prompt_char_budget=30_000
    )
    reuse = _admission_operation(
        reuse_daily,
        f"reused-{status}",
        reuse_quote,
        title=title.lower(),
        summary=summary.upper(),
    )

    accepted = compile_memory.apply_compile_batch(
        reuse_request,
        _response(reuse),
        False,
    )

    assert accepted["ok"] is True
    assert inactive.read_bytes() == original
    exact_quote = "An inactive exact target still cannot be created again."
    exact_daily = _daily(
        root / "knowledge" / "daily" / f"2026-09-2{2 if status == 'archived' else 3}.md",
        [_block("12:00:00", exact_quote)],
    )
    exact_request = compile_memory.prepare_compile_request(
        [exact_daily], state, prompt_char_budget=30_000
    )
    exact = _admission_operation(
        exact_daily,
        inactive.stem,
        exact_quote,
        title=f"Different {status} target title",
        summary=f"Different {status} target summary.",
    )

    rejected = compile_memory.apply_compile_batch(
        exact_request,
        _response(exact),
        False,
    )

    _assert_rejected_before_journal(
        rejected,
        exact_request,
        exact_daily,
        state_root,
        state,
    )
    assert inactive.read_bytes() == original


@pytest.mark.parametrize("status", ("archived", "superseded"))
def test_update_rejects_inactive_lifecycle_target_before_journal(
    compile_env,
    status,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Inactive knowledge cannot receive a new compiled update."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-09-12-update-{status}.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / f"inactive-update-{status}.md"
    _write_note(
        target,
        f"Inactive Update {status.title()}",
        "Inactive lifecycle target.",
        status=status,
    )
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "update target" in result["error"]
    assert target.read_bytes() == before


def test_update_rejects_category_that_differs_from_target_type(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Compiled updates stay inside the target's durable type."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-12-update-type.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "typed-update-target.md"
    _write_note(target, "Typed Update Target", "A pattern target.")
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        target.stem,
        quote,
        action="update",
    )
    operation["category"] = "decisions"

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "type" in result["error"]
    assert target.read_bytes() == before


def test_update_rejects_project_scoped_target_without_matching_scope(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Unscoped compilation cannot modify project-private knowledge."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-12-update-project.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "project-update-target.md"
    _write_note(target, "Project Update Target", "A project-private target.")
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "status: active\n",
            "status: active\nproject: private-project\n",
        ),
        encoding="utf-8",
    )
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "project" in result["error"]
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "status_key",
    ('"sta\\u0074us"', '"sta\\x74us"', '"sta\\U00000074us"'),
)
def test_admission_honors_escaped_inactive_status_key(compile_env, status_key):
    compile_memory, root, _state_root, state = compile_env
    inactive = root / "knowledge" / "notes" / "escaped-status-inactive.md"
    title = "Escaped Status Reusable Title"
    summary = "Escaped inactive status does not reserve this summary."
    _write_note(inactive, title, summary, status="archived")
    inactive.write_text(
        inactive.read_text(encoding="utf-8").replace(
            "status: archived",
            f"{status_key}: archived",
        ),
        encoding="utf-8",
    )
    original = inactive.read_bytes()
    quote = "Escaped inactive status keys have the same lifecycle semantics."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-13-escaped-status.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "escaped-status-reuse",
                quote,
                title=title,
                summary=summary,
            )
        ),
        False,
    )

    assert result["ok"] is True
    assert inactive.read_bytes() == original
    assert (root / "knowledge" / "notes" / "escaped-status-reuse.md").is_file()


@pytest.mark.parametrize(
    "status_lines",
    (
        '"sta\\u0074us: active',
        '"sta\\xG0tus": active',
        '"sta\\uD800tus": active',
        '"sta\\U00110000tus": active',
        'status: active\n"sta\\u0074us": active',
        'status: active\n"sta\\x74us": active',
        'status: active\n"sta\\U00000074us": active',
    ),
)
def test_admission_fails_closed_on_malformed_or_duplicate_decoded_status_key(
    compile_env,
    status_lines,
):
    compile_memory, root, state_root, state = compile_env
    existing = root / "knowledge" / "notes" / "invalid-escaped-status.md"
    _write_note(existing, "Invalid Escaped Status", "Status ambiguity is unsafe.")
    existing.write_text(
        existing.read_text(encoding="utf-8").replace(
            "status: active",
            status_lines,
        ),
        encoding="utf-8",
    )
    quote = "Ambiguous escaped lifecycle metadata fails closed before admission."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-13-invalid-status.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "invalid-escaped-status-proposal",
                quote,
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "metadata is malformed" in result["error"]
    assert not (
        root / "knowledge" / "notes" / "invalid-escaped-status-proposal.md"
    ).exists()


def test_lexically_archived_malformed_page_does_not_block_live_inventory(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    archived = root / "knowledge" / "notes" / "Archive" / "malformed.md"
    archived.parent.mkdir()
    archived.write_bytes(b"\xff\xfe\x00")
    quote = "Malformed archived content is outside the live uniqueness corpus."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-13-archive-skip.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "archive-skip-live-note",
                quote,
            )
        ),
        False,
    )

    assert result["ok"] is True
    assert (root / "knowledge" / "notes" / "archive-skip-live-note.md").is_file()
    assert archived.read_bytes() == b"\xff\xfe\x00"


@pytest.mark.parametrize(
    "uncertainty",
    ("malformed", "unreadable", "oversized", "reparse", "incomplete"),
)
def test_uncertain_active_note_inventory_fails_closed(
    compile_env,
    monkeypatch,
    uncertainty,
):
    import memory_state

    compile_memory, root, state_root, state = compile_env
    quote = "Uniqueness requires a complete readable live inventory."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    page = root / "knowledge" / "notes" / "uncertain-existing.md"
    _write_note(page, "Uncertain Existing", "An existing summary.")
    if uncertainty == "malformed":
        page.write_text(
            "---\ntype: pattern\nstatus: active\nstatus: active\n---\n\n"
            "# Malformed active page\n",
            encoding="utf-8",
        )
    elif uncertainty == "unreadable":
        real_read = compile_memory._read_knowledge_page
        monkeypatch.setattr(
            compile_memory,
            "_read_knowledge_page",
            lambda path: None if path == page else real_read(path),
        )
    elif uncertainty == "oversized":
        monkeypatch.setattr(compile_memory, "MAX_KNOWLEDGE_PAGE_BYTES", 64)
        page.write_text("# Oversized active page\n" + "x" * 256, encoding="utf-8")
    elif uncertainty == "reparse":
        monkeypatch.setattr(
            compile_memory,
            "bounded_path_inventory",
            lambda *_args, **_kwargs: memory_state.BoundedPathInventory((), error=True),
            raising=False,
        )
    else:
        monkeypatch.setattr(
            compile_memory,
            "bounded_path_inventory",
            lambda *_args, **_kwargs: memory_state.BoundedPathInventory(
                (page,), overflow=True
            ),
            raising=False,
        )
    target = root / "knowledge" / "notes" / f"uncertain-{uncertainty}.md"

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                f"uncertain-{uncertainty}",
                quote,
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "inventory" in result["error"]
    assert not target.exists()


def test_admission_rejects_page_swapped_to_external_symlink_after_inventory(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Live uniqueness cannot rely on content reached through a swapped link."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-symlink.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "identity-bound-source.md"
    _write_note(
        existing,
        "Identity Bound Duplicate",
        "The original live note reserves this title.",
    )
    outside = root.parent / "outside-note.md"
    _write_note(
        outside,
        "Unrelated External Title",
        "External content must not establish corpus uniqueness.",
    )
    probe = root.parent / "symlink-probe.md"
    try:
        probe.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    probe.unlink()
    outside_before = outside.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    real_inventory = compile_memory.bounded_path_inventory
    swapped = False

    def inventory_then_swap(*args, **kwargs):
        nonlocal swapped
        inventory = real_inventory(*args, **kwargs)
        if not swapped:
            existing.unlink()
            existing.symlink_to(outside)
            swapped = True
        return inventory

    monkeypatch.setattr(
        compile_memory,
        "bounded_path_inventory",
        inventory_then_swap,
    )
    target = root / "knowledge" / "notes" / "identity-bound-proposal.md"
    operation = _admission_operation(
        daily,
        target.stem,
        quote,
        title="identity bound duplicate",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "inventory" in result["error"]
    assert swapped is True
    assert existing.is_symlink()
    assert outside.read_bytes() == outside_before
    assert not target.exists()


def test_admission_rejects_modeled_reparse_swap_after_inventory(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Reparse content cannot establish live corpus uniqueness."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-reparse.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "reparse-source.md"
    _write_note(
        existing,
        "Reparse Bound Duplicate",
        "The lexical live note reserves this title.",
    )
    outside = root.parent / "modeled-external-note.md"
    _write_note(
        outside,
        "Unrelated Modeled External Title",
        "External content must not establish corpus uniqueness.",
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    real_inventory = compile_memory.bounded_path_inventory
    real_lstat = Path.lstat
    real_open = Path.open
    state_after_inventory = {"swapped": False}

    class ReparseMetadata:
        def __init__(self, metadata):
            self.metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            self.st_file_attributes = 0x400

        def __getattr__(self, name):
            return getattr(self.metadata, name)

    def inventory_then_swap(*args, **kwargs):
        inventory = real_inventory(*args, **kwargs)
        state_after_inventory["swapped"] = True
        return inventory

    def swapped_lstat(path, *args, **kwargs):
        metadata = real_lstat(path, *args, **kwargs)
        if path == existing and state_after_inventory["swapped"]:
            return ReparseMetadata(metadata)
        return metadata

    def swapped_open(path, *args, **kwargs):
        if path == existing and state_after_inventory["swapped"]:
            return real_open(outside, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(
        compile_memory,
        "bounded_path_inventory",
        inventory_then_swap,
    )
    monkeypatch.setattr(Path, "lstat", swapped_lstat)
    monkeypatch.setattr(Path, "open", swapped_open)
    target = root / "knowledge" / "notes" / "reparse-proposal.md"
    operation = _admission_operation(
        daily,
        target.stem,
        quote,
        title="reparse bound duplicate",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "inventory" in result["error"]
    assert state_after_inventory["swapped"] is True
    assert not target.exists()


def test_note_publication_is_bound_to_inventory_directory_identity(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "A validated notes directory must remain the publication directory."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-directory-swap.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    notes = root / "knowledge" / "notes"
    displaced = root / "knowledge" / "notes-displaced"
    real_inventory = compile_memory.bounded_path_inventory

    def inventory_then_replace_directory(*args, **kwargs):
        inventory = real_inventory(*args, **kwargs)
        notes.rename(displaced)
        notes.mkdir()
        return inventory

    monkeypatch.setattr(
        compile_memory,
        "bounded_path_inventory",
        inventory_then_replace_directory,
    )
    operation = _admission_operation(
        daily,
        "directory-bound-publication",
        quote,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    assert result["ok"] is False
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not (notes / "directory-bound-publication.md").exists()
    assert not (displaced / "directory-bound-publication.md").exists()


def test_create_does_not_overwrite_target_appearing_after_validation(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "A concurrent target must survive create publication unchanged."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-target-race.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    target = root / "knowledge" / "notes" / "target-race.md"
    real_atomic_write = compile_memory.atomic_write
    raced = False

    def target_appears_before_publish(path, content):
        nonlocal raced
        if Path(path) == target and not raced:
            target.write_text("CONCURRENT_TARGET\n", encoding="utf-8")
            raced = True
        real_atomic_write(path, content)

    monkeypatch.setattr(compile_memory, "atomic_write", target_appears_before_publish)

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, target.stem, quote)),
        False,
    )

    assert raced is True
    assert result["ok"] is False
    assert result["status"] == "apply_failed"
    assert target.read_text(encoding="utf-8") == "CONCURRENT_TARGET\n"
    assert daily.name not in state.get("compiled_daily_hashes", {})


def test_journaled_update_cas_preserves_concurrent_target_edit(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, state_root, state = compile_env
    quote = "An admitted update must not overwrite a concurrent target edit."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-update-cas.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "update-cas.md"
    _write_note(target, "Update CAS", "The admitted target bytes are immutable.")
    admitted = target.read_bytes()
    concurrent = admitted + b"\nCONCURRENT_EDIT\n"
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    real_exchange = memory_state._exchange_expected_base_files
    raced = False

    def edit_at_atomic_exchange(path, *args, **kwargs):
        nonlocal raced
        if Path(path) == target and not raced:
            target.write_bytes(concurrent)
            raced = True
        return real_exchange(path, *args, **kwargs)

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        edit_at_atomic_exchange,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        False,
    )

    journal = json.loads(
        (
            state_root
            / "run"
            / "compile-journal"
            / f"{request['batch_id']}.json"
        ).read_text(encoding="utf-8")
    )
    expected = journal["accepted"]["operations"][0]["_expected_target"]
    assert raced is True
    assert result["ok"] is False
    assert result["status"] == "apply_failed"
    assert expected["sha256"] == compile_memory.hashlib.sha256(admitted).hexdigest()
    assert expected["size"] == len(admitted)
    assert len(expected["identity"]) == 3
    assert journal["operation_states"] == ["pending"]
    assert result["recovery_required"] is True
    assert journal["status"] == "recovery_required"
    assert journal["operation_recovery"][0]["status"] == "prepared"
    assert target.read_bytes() == concurrent
    assert state["last_compile_sdk_error"]["stage"] == "apply"
    assert daily.name not in state.get("compiled_daily_hashes", {})


def test_direct_update_cas_rejects_same_bytes_identity_replacement(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    quote = "An admitted update is bound to the exact regular target identity."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-direct-update-cas.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "direct-update-cas.md"
    _write_note(target, "Direct Update CAS", "The target identity is admitted once.")
    admitted = target.read_bytes()
    request = compile_memory.build_compile_request([daily])
    request["source_blocks"] = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    real_exchange = memory_state._exchange_expected_base_files
    raced = False

    def replace_at_atomic_exchange(path, *args, **kwargs):
        nonlocal raced
        if Path(path) == target and not raced:
            replacement = target.with_name("replacement.md")
            replacement.write_bytes(admitted)
            os.replace(replacement, target)
            raced = True
        return real_exchange(path, *args, **kwargs)

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        replace_at_atomic_exchange,
    )

    touched, report = compile_memory._apply_compile_response(
        request,
        _response(
            _admission_operation(
                daily,
                target.stem,
                quote,
                action="update",
            )
        ),
        [daily],
        False,
    )

    assert raced is False
    assert touched == []
    assert "transaction failed" in report
    assert "durable compile journal" in report
    assert target.read_bytes() == admitted
    assert b"llm-wiki-compile-op:" not in admitted
    assert state["last_compile_sdk_error"]["stage"] == "apply"
    assert daily.name not in state.get("compiled_daily_hashes", {})


@pytest.mark.skipif(sys.platform != "win32", reason="ReplaceFileW is Windows-only")
def test_windows_conditional_publication_replaces_matching_base(compile_env):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "windows-conditional.md"
    target.write_text("admitted base\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        memory_state.conditional_atomic_write(
            target,
            "published replacement\n",
            expected,
        )

    assert target.read_text(encoding="utf-8") == "published replacement\n"
    assert not list(target.parent.glob(f".{target.name}.*.displaced"))
    assert not list(target.parent.glob(f".{target.name}.*.rejected"))


@pytest.mark.skipif(os.name != "posix", reason="native rename exchange is POSIX-only")
def test_posix_conditional_publication_replaces_matching_base(compile_env):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "posix-conditional.md"
    target.write_text("admitted base\n", encoding="utf-8")
    target.chmod(0o640)
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        memory_state.conditional_atomic_write(
            target,
            "published replacement\n",
            expected,
        )

    assert target.read_text(encoding="utf-8") == "published replacement\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    _assert_bounded_retained_conditional_artifacts(target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_conditional_publication_conflicts_on_concurrent_chmod(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "mode-race.md"
    original = b"admitted base\n"
    target.write_bytes(original)
    target.chmod(0o644)
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    real_exchange = memory_state._exchange_expected_base_files
    raced = False

    def chmod_before_exchange(path, *args, **kwargs):
        nonlocal raced
        if not raced:
            target.chmod(0o600)
            raced = True
        return real_exchange(path, *args, **kwargs)

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        chmod_before_exchange,
    )

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(memory_state.AtomicWriteConflictError):
            memory_state.conditional_atomic_write(target, "attempted\n", expected)

    assert raced is True
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_conditional_temp_collision_never_unlinks_preexisting_file(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "artifact-collision.md"
    target.write_text("admitted\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    token = "a" * 32
    collision = target.with_name(f".{target.name}.{token}.replacement")
    collision.write_bytes(b"preexisting user bytes\n")

    class FixedUuid:
        hex = token

    monkeypatch.setattr(memory_state.uuid, "uuid4", lambda: FixedUuid())

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(FileExistsError):
            memory_state.conditional_atomic_write(target, "attempted\n", expected)

    assert collision.read_bytes() == b"preexisting user bytes\n"
    assert target.read_text(encoding="utf-8") == "admitted\n"


def test_conditional_scratch_cleanup_keeps_path_replaced_after_exclusive_create(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "artifact-owner-race.md"
    target.write_text("admitted\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    real_snapshot = memory_state._snapshot_regular_file
    replacement_path = None

    def replace_owned_scratch(path, *args, **kwargs):
        nonlocal replacement_path
        candidate = Path(path)
        if candidate.name.endswith(".replacement"):
            replacement_path = candidate
            racer = candidate.with_name("scratch-racer.md")
            racer.write_bytes(b"concurrent user artifact\n")
            os.replace(racer, candidate)
            raise OSError("injected scratch snapshot failure")
        return real_snapshot(path, *args, **kwargs)

    monkeypatch.setattr(
        memory_state,
        "_snapshot_regular_file",
        replace_owned_scratch,
    )

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(OSError, match="scratch snapshot failure"):
            memory_state.conditional_atomic_write(target, "attempted\n", expected)

    assert replacement_path is not None
    assert replacement_path.read_bytes() == b"concurrent user artifact\n"
    assert target.read_text(encoding="utf-8") == "admitted\n"


def test_repeated_exchange_failures_stop_at_unresolved_artifact_cap(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "artifact-cap.md"
    target.write_text("admitted\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    tokens = iter(("1" * 32, "2" * 32, "3" * 32))
    exchanges = []
    syncs = []

    class SequencedUuid:
        def __init__(self, token):
            self.hex = token

    monkeypatch.setattr(
        memory_state,
        "MAX_RETAINED_CONDITIONAL_ARTIFACTS_PER_TARGET",
        2,
    )
    monkeypatch.setattr(
        memory_state.uuid,
        "uuid4",
        lambda: SequencedUuid(next(tokens)),
    )

    def fail_exchange(*_args, **_kwargs):
        exchanges.append(1)
        raise OSError("injected exchange error")

    monkeypatch.setattr(memory_state, "_exchange_expected_base_files", fail_exchange)
    monkeypatch.setattr(
        memory_state,
        "_sync_conditional_parent",
        lambda *_args: syncs.append(1),
    )
    with memory_state.bind_atomic_writes_to_directory(target.parent):
        for _attempt in range(2):
            with pytest.raises(memory_state.AtomicWriteRecoveryError):
                memory_state.conditional_atomic_write(target, "attempted\n", expected)
        with pytest.raises(
            OSError,
            match="per-target retained artifact count limit",
        ):
            memory_state.conditional_atomic_write(target, "attempted\n", expected)

    assert exchanges == [1, 1]
    assert syncs == [1, 1]
    artifacts = sorted(target.parent.glob(f".{target.name}.*.replacement"))
    assert len(artifacts) == 2
    assert all(path.read_text(encoding="utf-8") == "attempted\n" for path in artifacts)


@pytest.mark.parametrize("dir_fd", (None, 123))
def test_conditional_parent_sync_models_path_and_bound_descriptor(
    tmp_path,
    monkeypatch,
    dir_fd,
):
    import memory_state

    events = []
    monkeypatch.setattr(
        memory_state,
        "_sync_parent_directory",
        lambda path: events.append(("path", Path(path))),
    )
    monkeypatch.setattr(
        memory_state.os,
        "fsync",
        lambda descriptor: events.append(("fd", descriptor)),
    )

    memory_state._sync_conditional_parent(tmp_path / "target.md", dir_fd)

    assert events == ([('path', tmp_path / "target.md")] if dir_fd is None else [("fd", 123)])


def test_conditional_publication_rollback_failure_preserves_recovery_bytes(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "rollback-recovery.md"
    target.write_text("admitted base\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    concurrent = b"concurrent replacement\n"
    real_exchange = memory_state._exchange_expected_base_files
    calls = 0

    def race_then_fail_rollback(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            racer = target.with_name("racer.md")
            racer.write_bytes(concurrent)
            os.replace(racer, target)
            return real_exchange(path, *args, **kwargs)
        raise OSError("injected rollback failure")

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        race_then_fail_rollback,
    )

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(memory_state.AtomicWriteRollbackError) as caught:
            memory_state.conditional_atomic_write(
                target,
                "rejected publication\n",
                expected,
            )

    recovery_paths = [Path(path) for path in caught.value.recovery_paths]
    assert calls == 2
    assert any(path.is_file() and path.read_bytes() == concurrent for path in recovery_paths)
    assert target.read_text(encoding="utf-8") == "rejected publication\n"
    assert caught.value.recovery_state["status"] == "required"
    assert caught.value.recovery_state["kind"] == "rollback"


def _journaled_update_case(compile_env, suffix):
    compile_memory, root, state_root, state = compile_env
    quote = f"Prepared recovery intent protects update {suffix}."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-01-{suffix}.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / f"prepared-{suffix}.md"
    _write_note(target, "Prepared Update", "The admitted base remains durable.")
    original = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = _response(
        _admission_operation(
            daily,
            target.stem,
            quote,
            action="update",
            body=f"UPDATE_{suffix.upper()}\n\n{_word_body(150)}",
        )
    )
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    return {
        "compile_memory": compile_memory,
        "root": root,
        "state_root": state_root,
        "state": state,
        "daily": daily,
        "target": target,
        "original": original,
        "request": request,
        "response": response,
        "journal_path": journal_path,
    }


def _assert_bounded_retained_conditional_artifacts(target):
    import memory_state

    artifacts = list(target.parent.glob(f".{target.name}.*"))
    if os.name != "posix":
        assert artifacts == []
        return
    assert (
        1
        <= len(artifacts)
        <= memory_state.MAX_RETAINED_CONDITIONAL_ARTIFACTS_PER_TARGET
    )
    assert sum(path.stat().st_size for path in artifacts) <= (
        memory_state.MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_PER_TARGET
    )
    assert all(path.is_file() and not path.is_symlink() for path in artifacts)


def test_update_recovery_intent_is_durable_before_native_exchange(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "intent")
    real_exchange = memory_state._exchange_expected_base_files
    inspected = False

    def inspect_prepared_journal(path, replacement, backup, *, dir_fd):
        nonlocal inspected
        if Path(path) != case["target"]:
            return real_exchange(path, replacement, backup, dir_fd=dir_fd)
        journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
        recovery = journal["operation_recovery"][0]
        operation = journal["accepted"]["operations"][0]
        disclosed = {
            recovery["replacement_path"],
            recovery["displaced_path"],
            recovery["rollback_backup_path"],
        }
        artifacts = {
            item.name
            for item in case["target"].parent.glob(f".{case['target'].name}.*")
        }

        assert recovery["status"] == "prepared"
        assert recovery["target"] == str(case["target"].absolute())
        assert recovery["expected"] == operation["_expected_target"]
        assert recovery["operation_fingerprint"] == (
            case["compile_memory"]._operation_replay_fingerprint(operation)
        )
        assert artifacts <= disclosed
        assert Path(replacement).name == recovery["replacement_path"]
        assert Path(backup).name == recovery["displaced_path"]
        inspected = True
        return real_exchange(path, replacement, backup, dir_fd=dir_fd)

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        inspect_prepared_journal,
    )

    result = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert inspected is True
    assert result["ok"] is True
    assert journal["operation_states"] == ["applied"]
    assert journal["operation_recovery"] == [None]
    _assert_bounded_retained_conditional_artifacts(case["target"])


def test_conditional_prepare_reserves_names_without_creating_artifacts(
    compile_env,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "reservation-only.md"
    target.write_text("admitted\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        reservation = memory_state.prepare_conditional_atomic_write(
            target,
            "attempted\n",
            expected,
            "reservation-fingerprint",
        )

    assert reservation["status"] == "prepared"
    assert reservation["replacement_path"].endswith(".replacement")
    assert not list(target.parent.glob(f".{target.name}.*"))


def test_eight_prepared_reservations_do_not_consume_artifact_capacity(
    compile_env,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "reservation-cap.md"
    target.write_text("admitted\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        reservations = [
            memory_state.prepare_conditional_atomic_write(
                target,
                "attempted\n",
                expected,
                f"reservation-{index}",
            )
            for index in range(8)
        ]

    assert len({item["token"] for item in reservations}) == 8
    assert not list(target.parent.glob(f".{target.name}.*"))


def test_compile_journal_models_strict_file_then_parent_sync(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, _state = compile_env
    (state_root / "run" / "compile-journal").mkdir()
    events = []
    monkeypatch.setattr(
        compile_memory,
        "atomic_write",
        lambda path, content: events.append(("atomic", Path(path), content)),
    )
    monkeypatch.setattr(
        compile_memory,
        "sync_file_strict",
        lambda path: events.append(("file", Path(path))),
        raising=False,
    )
    monkeypatch.setattr(
        compile_memory,
        "sync_parent_directory_strict",
        lambda path: events.append(("parent", Path(path))),
        raising=False,
    )

    compile_memory._write_journal({"batch_id": "a" * 64})

    journal = state_root / "run" / "compile-journal" / f"{'a' * 64}.json"
    assert [item[0] for item in events] == [
        "atomic",
        "file",
        "parent",
        "parent",
        "parent",
    ]
    assert [item[1] for item in events[2:]] == [
        journal,
        journal.parent,
        journal.parent.parent,
    ]


def test_prepared_journal_strict_sync_failure_prevents_exchange_and_artifacts(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "journal-sync")
    exchanges = 0
    real_exchange = memory_state._exchange_expected_base_files

    def count_exchange(*args, **kwargs):
        nonlocal exchanges
        exchanges += 1
        return real_exchange(*args, **kwargs)

    def fail_prepared_parent_sync(path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        recovery = payload.get("operation_recovery") or []
        if recovery and isinstance(recovery[0], dict):
            raise OSError("injected strict journal parent sync failure")

    monkeypatch.setattr(memory_state, "_exchange_expected_base_files", count_exchange)
    monkeypatch.setattr(
        case["compile_memory"],
        "sync_parent_directory_strict",
        fail_prepared_parent_sync,
        raising=False,
    )

    result = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    assert result["ok"] is False
    assert exchanges == 0
    assert case["target"].read_bytes() == case["original"]
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})
    assert not list(case["target"].parent.glob(f".{case['target'].name}.*"))


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
@pytest.mark.parametrize("failure", ("open", "fsync"))
def test_strict_parent_directory_sync_propagates_open_and_fsync_failures(
    tmp_path,
    monkeypatch,
    failure,
):
    import memory_state

    strict_sync = getattr(memory_state, "sync_parent_directory_strict", None)
    assert strict_sync is not None
    target = tmp_path / "journal.json"
    target.write_text("{}", encoding="utf-8")
    real_open = memory_state.os.open
    real_fsync = memory_state.os.fsync
    directory_fds = set()

    def fail_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path and failure == "open":
            raise OSError("injected directory open failure")
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == tmp_path:
            directory_fds.add(descriptor)
        return descriptor

    def fail_fsync(descriptor):
        if descriptor in directory_fds and failure == "fsync":
            raise OSError("injected directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(memory_state.os, "open", fail_open)
    monkeypatch.setattr(memory_state.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match=failure):
        strict_sync(target)


@pytest.mark.skipif(os.name == "posix", reason="POSIX retires without cleanup exchange")
def test_final_cleanup_exchange_restores_foreign_artifact_and_blocks_hash(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "cleanup-owner")
    real_exchange = memory_state._exchange_expected_base_files
    foreign = b"FOREIGN CLEANUP OWNER\n"
    exchanges = 0
    cleanup_artifact = None

    def replace_public_artifact_before_cleanup(path, replacement, backup, *, dir_fd):
        nonlocal exchanges, cleanup_artifact
        exchanges += 1
        if exchanges == 2:
            cleanup_artifact = Path(path)
            assert cleanup_artifact != case["target"]
            journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
            assert journal["operation_states"] == ["cleanup_pending"]
            racer = cleanup_artifact.with_name("foreign-cleanup-owner.md")
            racer.write_bytes(foreign)
            os.replace(racer, cleanup_artifact)
        return real_exchange(
            path,
            replacement,
            backup,
            dir_fd=dir_fd,
        )

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        replace_public_artifact_before_cleanup,
    )

    result = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert result["status"] == "apply_failed"
    assert result["recovery_required"] is True
    assert exchanges == 3
    assert cleanup_artifact is not None
    assert cleanup_artifact.read_bytes() == foreign
    assert b"UPDATE_CLEANUP-OWNER" in case["target"].read_bytes()
    assert journal["operation_states"] == ["cleanup_pending"]
    assert journal["operation_recovery"][0]["status"] == "cleanup_pending"
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})


def test_cleanup_parent_sync_failure_retries_before_hash_publication(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "cleanup-sync")
    real_sync = memory_state._sync_conditional_parent
    failed = False

    def fail_once_during_cleanup(path, dir_fd):
        nonlocal failed
        journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
        if not failed and journal["operation_states"] == ["cleanup_pending"]:
            failed = True
            raise OSError("injected cleanup parent sync failure")
        return real_sync(path, dir_fd)

    monkeypatch.setattr(
        memory_state,
        "_sync_conditional_parent",
        fail_once_during_cleanup,
    )

    first = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )
    first_journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    hashed_after_first = case["daily"].name in case["state"].get(
        "compiled_daily_hashes", {}
    )
    retry = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    final_journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert failed is True
    assert first["status"] == "apply_failed"
    assert first["recovery_required"] is True
    assert first_journal["operation_states"] == ["cleanup_pending"]
    assert first_journal["operation_recovery"][0]["status"] == "cleanup_pending"
    assert hashed_after_first is False
    assert retry["ok"] is True
    assert final_journal["operation_states"] == ["applied"]
    assert final_journal["operation_recovery"] == [None]
    assert case["state"]["compiled_daily_hashes"][case["daily"].name] == (
        case["request"]["dailies"][0]["sha256"]
    )
    _assert_bounded_retained_conditional_artifacts(case["target"])


def test_real_update_without_journal_fails_closed_but_dry_run_stays_read_only(
    compile_env,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Unjournaled updates must fail closed."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-01-nojournal.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "unjournaled-update.md"
    _write_note(target, "Unjournaled", "The original bytes stay unchanged.")
    original = target.read_bytes()
    request = compile_memory.build_compile_request([daily])
    request["source_blocks"] = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )
    response = _response(
        _admission_operation(daily, target.stem, quote, action="update")
    )

    dry_touched, dry_report = compile_memory._apply_compile_response(
        request, response, [daily], True
    )
    touched, report = compile_memory._apply_compile_response(
        request, response, [daily], False
    )

    assert dry_touched == [f"knowledge/notes/{target.name}"]
    assert "COMPILE_DONE" in dry_report
    assert touched == []
    assert "journal" in report.casefold()
    assert target.read_bytes() == original


def test_attempted_snapshot_failure_after_exchange_is_typed_and_restores_concurrent_bytes(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "snapshot")
    concurrent = case["original"] + b"\nCONCURRENT SNAPSHOT OWNER\n"
    real_exchange = memory_state._exchange_expected_base_files
    real_snapshot = memory_state._snapshot_regular_file
    exchanges = 0
    failed_snapshot = False

    def race_before_exchange(path, *args, **kwargs):
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            racer = case["target"].with_name("snapshot-racer.md")
            racer.write_bytes(concurrent)
            os.replace(racer, case["target"])
        return real_exchange(path, *args, **kwargs)

    def fail_attempted_snapshot(path, *args, **kwargs):
        nonlocal failed_snapshot
        candidate = Path(path)
        if candidate == case["target"] and b"llm-wiki-compile-op:" in candidate.read_bytes():
            failed_snapshot = True
            raise OSError("injected attempted snapshot failure")
        return real_snapshot(path, *args, **kwargs)

    monkeypatch.setattr(memory_state, "_exchange_expected_base_files", race_before_exchange)
    monkeypatch.setattr(memory_state, "_snapshot_regular_file", fail_attempted_snapshot)

    first = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )
    monkeypatch.setattr(memory_state, "_exchange_expected_base_files", real_exchange)
    monkeypatch.setattr(memory_state, "_snapshot_regular_file", real_snapshot)
    retry = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    assert failed_snapshot is True
    assert first["status"] == "apply_failed"
    assert first["recovery_required"] is True
    assert "UnboundLocalError" not in first["error"]
    assert retry["status"] == "apply_failed"
    assert case["target"].read_bytes() == concurrent
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})


def test_final_parent_sync_failure_rolls_back_before_retrying_once(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "sync")
    real_sync = memory_state._sync_conditional_parent
    failed = False

    def fail_once_after_exchange(path, dir_fd):
        nonlocal failed
        if not failed and b"llm-wiki-compile-op:" in case["target"].read_bytes():
            failed = True
            raise OSError("injected final parent sync failure")
        return real_sync(path, dir_fd)

    monkeypatch.setattr(memory_state, "_sync_conditional_parent", fail_once_after_exchange)

    first = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )
    target_after_first = case["target"].read_bytes()
    hashed_after_first = case["daily"].name in case["state"].get(
        "compiled_daily_hashes", {}
    )
    retry = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    content = case["target"].read_text(encoding="utf-8")
    assert failed is True
    assert first["status"] == "apply_failed"
    assert first["recovery_required"] is True
    assert target_after_first == case["original"]
    assert hashed_after_first is False
    assert retry["ok"] is True
    assert content.count("UPDATE_SYNC") == 1
    assert case["state"]["compiled_daily_hashes"][case["daily"].name] == (
        case["request"]["dailies"][0]["sha256"]
    )


def _run_hard_crash_update(case, crash_point):
    request_path = case["state_root"] / "request.json"
    response_path = case["state_root"] / "response.json"
    state_path = case["state_root"] / "run" / "state.json"
    request_path.write_text(json.dumps(case["request"]), encoding="utf-8")
    response_path.write_text(case["response"], encoding="utf-8")
    state_path.write_text(json.dumps(case["state"]), encoding="utf-8")
    script = r'''
import json
import os
import sys
from pathlib import Path

import compile_memory
import memory_state

point = sys.argv[1]
target = Path(sys.argv[2])
journal_path = Path(sys.argv[3])
request = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
response = Path(sys.argv[5]).read_text(encoding="utf-8")
def rebuild_index():
    links = [
        f"- [[knowledge/notes/{path.stem}]]"
        for path in sorted(compile_memory.KNOWLEDGE.glob("*.md"))
    ]
    compile_memory.INDEX.write_text("\n".join(links) + "\n", encoding="utf-8")
    return True
compile_memory.rebuild_index = rebuild_index

real_write_journal = compile_memory._write_journal
if point in {"prepared", "applied"}:
    def crash_after_journal(journal):
        real_write_journal(journal)
        recovery = (journal.get("operation_recovery") or [None])[0]
        if point == "prepared" and isinstance(recovery, dict) and recovery.get("status") == "prepared":
            os._exit(71)
        if point == "applied" and journal.get("operation_states") == ["applied"] and isinstance(recovery, dict):
            os._exit(74)
    compile_memory._write_journal = crash_after_journal

real_exchange = memory_state._exchange_expected_base_files
if point == "exchange":
    def crash_after_exchange(*args, **kwargs):
        displaced = real_exchange(*args, **kwargs)
        os._exit(72)
    memory_state._exchange_expected_base_files = crash_after_exchange
elif point == "rollback":
    exchanges = 0
    def crash_during_rollback(path, *args, **kwargs):
        global exchanges
        exchanges += 1
        if exchanges == 1:
            racer = target.with_name("hard-crash-concurrent.md")
            racer.write_bytes(b"CONCURRENT HARD CRASH OWNER\n")
            os.replace(racer, target)
            return real_exchange(path, *args, **kwargs)
        os._exit(75)
    memory_state._exchange_expected_base_files = crash_during_rollback

if point == "validated":
    real_assert = memory_state._assert_expected_base_file
    validations = 0
    def crash_after_displaced_validation(*args, **kwargs):
        global validations
        real_assert(*args, **kwargs)
        validations += 1
        if validations == 1:
            os._exit(73)
    memory_state._assert_expected_base_file = crash_after_displaced_validation

result = compile_memory.apply_compile_batch(request, response, False)
print(json.dumps(result), flush=True)
'''
    env = os.environ.copy()
    env["LLM_WIKI_ROOT"] = str(case["root"])
    env["LLM_WIKI_STATE_ROOT"] = str(case["state_root"])
    env["MEMORY_COMPILE_PROMPT_CHAR_BUDGET"] = "30000"
    scripts = str(Path(case["compile_memory"].__file__).resolve().parent)
    env["PYTHONPATH"] = scripts + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            crash_point,
            str(case["target"]),
            str(case["journal_path"]),
            str(request_path),
            str(response_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )


def _retry_hard_crash_update(case):
    script = r'''
import json
import sys
from pathlib import Path
import compile_memory
def rebuild_index():
    links = [
        f"- [[knowledge/notes/{path.stem}]]"
        for path in sorted(compile_memory.KNOWLEDGE.glob("*.md"))
    ]
    compile_memory.INDEX.write_text("\n".join(links) + "\n", encoding="utf-8")
    return True
compile_memory.rebuild_index = rebuild_index
request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
response = Path(sys.argv[2]).read_text(encoding="utf-8")
print(json.dumps(compile_memory.apply_compile_batch(request, response, False)), flush=True)
'''
    env = os.environ.copy()
    env["LLM_WIKI_ROOT"] = str(case["root"])
    env["LLM_WIKI_STATE_ROOT"] = str(case["state_root"])
    env["MEMORY_COMPILE_PROMPT_CHAR_BUDGET"] = "30000"
    scripts = str(Path(case["compile_memory"].__file__).resolve().parent)
    env["PYTHONPATH"] = scripts + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(case["state_root"] / "request.json"),
            str(case["state_root"] / "response.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("crash_point", "exit_code", "normal_exchange"),
    (
        ("prepared", 71, True),
        ("exchange", 72, True),
        ("validated", 73, True),
        ("applied", 74, True),
        ("rollback", 75, False),
    ),
)
def test_journaled_update_survives_subprocess_hard_crash_at_each_protocol_boundary(
    compile_env,
    crash_point,
    exit_code,
    normal_exchange,
):
    case = _journaled_update_case(compile_env, "hard")

    crashed = _run_hard_crash_update(case, crash_point)

    assert crashed.returncode == exit_code, (crashed.stdout, crashed.stderr)
    crashed_journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    recovery = crashed_journal["operation_recovery"][0]
    assert recovery["status"] == (
        "cleanup_pending" if crash_point == "applied" else "prepared"
    )
    assert recovery["operation_fingerprint"]
    if crash_point == "prepared":
        assert not list(case["target"].parent.glob(f".{case['target'].name}.*"))
    assert case["daily"].name not in json.loads(
        (case["state_root"] / "run" / "state.json").read_text(encoding="utf-8")
        if (case["state_root"] / "run" / "state.json").exists()
        else "{}"
    ).get("compiled_daily_hashes", {})

    retry = _retry_hard_crash_update(case)
    state_path = case["state_root"] / "run" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    content = case["target"].read_text(encoding="utf-8")

    if normal_exchange:
        assert retry["ok"] is True
        assert content.count("UPDATE_HARD") == 1
        assert state["compiled_daily_hashes"][case["daily"].name] == (
            case["request"]["dailies"][0]["sha256"]
        )
        journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
        assert journal["operation_states"] == ["applied"]
        assert journal["operation_recovery"] == [None]
        _assert_bounded_retained_conditional_artifacts(case["target"])
    else:
        assert retry["ok"] is False
        assert case["target"].read_bytes() == b"CONCURRENT HARD CRASH OWNER\n"
        assert case["daily"].name not in state.get("compiled_daily_hashes", {})


def _start_journaled_rollback_failure(
    compile_env,
    monkeypatch,
    suffix,
    *,
    crash_on_required=False,
):
    import memory_state

    compile_memory, root, state_root, state = compile_env
    quote = f"Rollback recovery is journal durable for {suffix}."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-09-25-{suffix}.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / f"journal-recovery-{suffix}.md"
    _write_note(target, "Journal Recovery", "The admitted target predates the race.")
    concurrent = (
        target.read_bytes()
        + b"\nConcurrent writer owns these canonical bytes without an operation marker.\n"
    )
    request = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=30_000,
    )
    response = _response(
        _admission_operation(
            daily,
            target.stem,
            quote,
            action="update",
        )
    )
    real_exchange = memory_state._exchange_expected_base_files
    calls = 0

    def race_then_fail_rollback(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            racer = target.with_name(f"racer-{suffix}.md")
            racer.write_bytes(concurrent)
            os.replace(racer, target)
            return real_exchange(path, *args, **kwargs)
        raise OSError("injected journal rollback failure")

    rebuilds = []
    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        race_then_fail_rollback,
    )
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: rebuilds.append(1) or _rebuild_test_index(compile_memory),
    )
    real_write_journal = compile_memory._write_journal
    required_crashed = False
    if crash_on_required:
        def crash_after_required(journal):
            nonlocal required_crashed
            real_write_journal(journal)
            recovery = journal.get("operation_recovery") or []
            if (
                not required_crashed
                and recovery
                and isinstance(recovery[0], dict)
                and recovery[0].get("status") == "required"
            ):
                required_crashed = True
                raise SystemExit("crash after required")

        monkeypatch.setattr(
            compile_memory,
            "_write_journal",
            crash_after_required,
        )
    try:
        first = compile_memory.apply_compile_batch(request, response, False)
    except SystemExit:
        if not required_crashed:
            raise
        first = {"ok": False, "status": "crashed"}
    finally:
        monkeypatch.setattr(
            memory_state,
            "_exchange_expected_base_files",
            real_exchange,
        )
        monkeypatch.setattr(
            compile_memory,
            "_write_journal",
            real_write_journal,
        )
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    return {
        "compile_memory": compile_memory,
        "state": state,
        "daily": daily,
        "target": target,
        "concurrent": concurrent,
        "request": request,
        "response": response,
        "first": first,
        "journal": journal,
        "journal_path": journal_path,
        "rebuilds": rebuilds,
        "required_crashed": required_crashed,
    }


def test_journal_recovery_restores_concurrent_target_before_retry(
    compile_env,
    monkeypatch,
):
    case = _start_journaled_rollback_failure(
        compile_env,
        monkeypatch,
        "normal-retry",
    )
    compile_memory = case["compile_memory"]

    assert case["first"]["status"] == "apply_failed"
    assert case["journal"]["status"] == "recovery_required"
    assert case["journal"]["operation_states"] == ["pending"]
    assert case["journal"]["operation_recovery"][0]["status"] == "required"
    assert b"llm-wiki-compile-op:" in case["target"].read_bytes()

    retry = compile_memory.apply_compile_batch(
        case["request"],
        case["response"],
        False,
    )

    recovered = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert retry["status"] == "apply_failed"
    assert case["target"].read_bytes() == case["concurrent"]
    assert recovered["operation_states"] == ["pending"]
    assert recovered["operation_recovery"][0]["status"] == "resolved"
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})
    assert case["rebuilds"] == []


def test_rollback_cleanup_replacement_race_preserves_foreign_bytes(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _start_journaled_rollback_failure(
        compile_env,
        monkeypatch,
        "cleanup-rebind",
    )
    real_retire = memory_state._retire_open_descriptor
    foreign = b"FOREIGN ROLLBACK ARTIFACT\n"
    raced_path = None

    def replace_after_check(path, descriptor, replacement):
        nonlocal raced_path
        candidate = Path(path)
        if raced_path is None and candidate != case["target"]:
            raced_path = candidate
            checked = candidate.with_name("checked-rollback-cleanup.md")
            racer = candidate.with_name("rollback-cleanup-racer.md")
            racer.write_bytes(foreign)
            os.replace(candidate, checked)
            os.replace(racer, candidate)
        return real_retire(path, descriptor, replacement)

    monkeypatch.setattr(memory_state, "_retire_open_descriptor", replace_after_check)

    retry = case["compile_memory"].apply_compile_batch(
        case["request"],
        case["response"],
        False,
    )

    assert raced_path is not None
    assert retry["status"] == "apply_failed"
    assert retry["recovery_required"] is True
    assert raced_path.read_bytes() == foreign
    assert case["target"].read_bytes() == case["concurrent"]
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})


def test_journal_recovery_is_retry_safe_after_required_transition_crash(
    compile_env,
    monkeypatch,
):
    case = _start_journaled_rollback_failure(
        compile_env,
        monkeypatch,
        "crash-required",
        crash_on_required=True,
    )
    compile_memory = case["compile_memory"]
    assert case["required_crashed"] is True
    assert case["first"]["status"] == "crashed"
    assert case["journal"]["status"] == "recovery_required"
    assert case["journal"]["operation_recovery"][0]["status"] == "required"

    retry = compile_memory.apply_compile_batch(
        case["request"],
        case["response"],
        False,
    )

    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert retry["status"] == "apply_failed"
    assert case["target"].read_bytes() == case["concurrent"]
    assert journal["operation_states"] == ["pending"]
    assert journal["operation_recovery"][0]["status"] == "resolved"
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})
    assert case["rebuilds"] == []


def test_journal_recovery_remains_required_when_restore_cannot_complete(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _start_journaled_rollback_failure(
        compile_env,
        monkeypatch,
        "restore-failure",
    )
    compile_memory = case["compile_memory"]
    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected recovery exchange failure")
        ),
    )

    retry = compile_memory.apply_compile_batch(
        case["request"],
        case["response"],
        False,
    )

    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert retry["status"] == "apply_failed"
    assert retry["recovery_required"] is True
    assert journal["status"] == "recovery_required"
    assert journal["operation_states"] == ["pending"]
    assert journal["operation_recovery"][0]["status"] == "restoring"
    assert b"llm-wiki-compile-op:" in case["target"].read_bytes()
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})
    assert case["rebuilds"] == []


def test_recovery_required_journal_without_metadata_never_trusts_marker(
    compile_env,
    monkeypatch,
):
    case = _start_journaled_rollback_failure(
        compile_env,
        monkeypatch,
        "missing-meta",
    )
    compile_memory = case["compile_memory"]
    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    journal.pop("operation_recovery")
    journal["status"] = "recovery_required"
    compile_memory._write_journal(journal)
    attempted = case["target"].read_bytes()
    assert b"llm-wiki-compile-op:" in attempted

    retry = compile_memory.apply_compile_batch(
        case["request"],
        case["response"],
        False,
    )

    persisted = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert retry["status"] == "journal_error"
    assert "fields are invalid" in retry["error"]
    assert case["target"].read_bytes() == attempted
    assert persisted["status"] == "recovery_required"
    assert persisted["operation_states"] == ["pending"]
    assert "operation_recovery" not in persisted
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})
    assert case["rebuilds"] == []


@pytest.mark.parametrize("crash_status", ("restoring", "restored", "resolved"))
def test_journal_recovery_is_retry_safe_after_each_transition_crash(
    compile_env,
    monkeypatch,
    crash_status,
):
    case = _start_journaled_rollback_failure(
        compile_env,
        monkeypatch,
        f"crash-{crash_status}",
    )
    compile_memory = case["compile_memory"]
    real_write_journal = compile_memory._write_journal
    crashed = False

    def crash_after_durable_transition(journal):
        nonlocal crashed
        real_write_journal(journal)
        recovery = journal.get("operation_recovery") or []
        if (
            not crashed
            and recovery
            and isinstance(recovery[0], dict)
            and recovery[0].get("status") == crash_status
        ):
            crashed = True
            raise SystemExit(f"crash after {crash_status}")

    monkeypatch.setattr(
        compile_memory,
        "_write_journal",
        crash_after_durable_transition,
    )
    with pytest.raises(SystemExit, match=crash_status):
        compile_memory.apply_compile_batch(
            case["request"],
            case["response"],
            False,
        )
    monkeypatch.setattr(compile_memory, "_write_journal", real_write_journal)

    retry = compile_memory.apply_compile_batch(
        case["request"],
        case["response"],
        False,
    )

    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert crashed is True
    assert retry["status"] == "apply_failed"
    assert case["target"].read_bytes() == case["concurrent"]
    assert journal["operation_states"] == ["pending"]
    assert journal["operation_recovery"][0]["status"] == "resolved"
    assert case["daily"].name not in case["state"].get("compiled_daily_hashes", {})
    assert case["rebuilds"] == []


def test_conditional_rollback_never_deletes_intervening_writer_bytes(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "rollback-third-writer.md"
    target.write_text("admitted base\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    concurrent = b"first concurrent writer\n"
    intervening = b"intervening third writer\n"
    real_exchange = memory_state._exchange_expected_base_files
    calls = 0

    def race_both_exchanges(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        racer = target.with_name(f"racer-{calls}.md")
        if calls == 1:
            racer.write_bytes(concurrent)
            os.replace(racer, target)
        elif calls == 2:
            racer.write_bytes(intervening)
            os.replace(racer, target)
        return real_exchange(path, *args, **kwargs)

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        race_both_exchanges,
    )

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(memory_state.AtomicWriteRecoveryError) as caught:
            memory_state.conditional_atomic_write(
                target,
                "rejected publication\n",
                expected,
            )

    recovery_paths = [Path(path) for path in caught.value.recovery_paths]
    assert calls == 2
    assert target.read_bytes() == concurrent
    assert any(
        path.is_file() and path.read_bytes() == intervening
        for path in recovery_paths
    )


@pytest.mark.skipif(sys.platform != "win32", reason="ReplaceFileW errors are Windows-only")
@pytest.mark.parametrize("winerror", (1175, 1176, 1177))
def test_windows_conditional_publication_preserves_bytes_for_documented_errors(
    compile_env,
    monkeypatch,
    winerror,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / f"replace-error-{winerror}.md"
    original = b"original target\n"
    replacement = b"attempted replacement\n"
    target.write_bytes(original)
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]

    def fail_replace(replaced, replacing, backup):
        if winerror == 1177:
            os.replace(replaced, backup)
        raise OSError(winerror, f"injected ReplaceFileW error {winerror}")

    monkeypatch.setattr(memory_state, "_replace_file_windows", fail_replace)

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(memory_state.AtomicWriteRecoveryError) as caught:
            memory_state.conditional_atomic_write(target, replacement.decode(), expected)

    preserved = {
        path.read_text(encoding="utf-8")
        for path in map(Path, caught.value.recovery_paths)
        if path.is_file()
    }
    if target.is_file():
        preserved.add(target.read_text(encoding="utf-8"))
    assert original.decode() in preserved
    assert replacement.decode() in preserved


def test_direct_manual_apply_cannot_bypass_admission(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Direct dry-run reporting must use live admission checks."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-15.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "manual-existing.md"
    _write_note(existing, "Manual Existing", "Manual path existing summary.")
    before = existing.read_bytes()
    request = compile_memory.build_compile_request([daily])
    request["source_blocks"] = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    touched, report = compile_memory._apply_compile_response(
        request,
        _response(_admission_operation(daily, "manual-existing", quote)),
        [daily],
        True,
    )

    assert touched == []
    assert "invalid provider plan" in report
    assert "create target" in report
    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert existing.read_bytes() == before


def test_plan_rejects_two_updates_to_one_canonical_target_before_journal(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    quote = "A compile plan may mutate each canonical note target only once."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-duplicate-updates.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "duplicate-update.md"
    _write_note(target, "Duplicate Update", "The target must remain all-or-nothing.")
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operations = [
        _admission_operation(
            daily,
            target.stem,
            quote,
            action="update",
            body=f"update body {index}",
        )
        for index in range(2)
    ]

    result = compile_memory.apply_compile_batch(
        request,
        _response(*operations),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "duplicate mutation target" in result["error"]
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "actions",
    (("create", "update"), ("update", "create")),
)
def test_plan_rejects_mixed_duplicate_mutation_targets_before_journal(
    compile_env,
    actions,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Mixed actions cannot alias one canonical note destination."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-14-mixed-duplicates.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "mixed-duplicate.md"
    _write_note(target, "Mixed Duplicate", "The existing note remains unchanged.")
    before = target.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operations = [
        _admission_operation(
            daily,
            target.stem,
            quote,
            action=action,
            body=f"{action} duplicate body",
        )
        for action in actions
    ]

    result = compile_memory.apply_compile_batch(
        request,
        _response(*operations),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "duplicate mutation target" in result["error"]
    assert target.read_bytes() == before


def test_idle_quote_ambiguous_between_durable_and_nondurable_sections_is_rejected(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    quote = "When repeated text appears in two contexts, reject source ambiguity."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-16.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "**Work completed**\n"
                f"- {quote}\n\n"
                "**Lessons / patterns**\n"
                f"- {quote}",
            )
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "ambiguous-section", quote)),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "ambiguous" in result["error"]


def test_idle_status_citation_is_rejected_for_updates(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "A status citation cannot update an existing durable target."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-17.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"{quote}\n\n**Lessons / patterns**\n"
                "- Only admitted durable bullets may support an update.",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "compatible-update.md"
    _write_note(target, "Compatible Update", "The existing page can receive updates.")
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "compatible-update",
        quote,
        action="update",
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation, audit={"verified": 999, "dedup": 999}),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "not an exact source citation" in result["error"]


def test_legacy_unscoped_source_is_excluded_while_scoped_source_remains(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    session_quote = "Canonical session-end evidence remains compatible."
    legacy_quote = "Legacy unscoped deferred evidence remains compatible."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-20.md",
        [
            _block("12:00:00", session_quote),
            f"## [12:01:00] deferred-pre-compact\n{legacy_quote}",
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    assert len(source_blocks) == 1
    assert session_quote in source_blocks[0]
    assert legacy_quote not in source_blocks[0]

    operation = _admission_operation(
        daily,
        "canonical-session-end",
        session_quote,
        timestamp="12:00:00",
    )
    plan, error = compile_memory._normalize_accepted_plan(
        _response(operation, audit={"verified": 999, "dedup": 999}),
        [daily],
        source_blocks,
    )

    assert error == ""
    assert plan is not None
    assert plan["audit"]["verified"] == 1
    assert plan["audit"]["dedup"] == 3


def test_fuzzy_only_pair_is_report_only_and_does_not_supersede(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Fuzzy candidates are reported without mutating established history."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-18.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "durable-compile-policy.md"
    _write_note(
        existing,
        "Durable Compile Admission Policy",
        "The established policy remains active.",
    )
    before = existing.read_bytes()
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    body = "Use a durable compile admission replacement instead of automatic mutation. "
    body += _word_body(150)
    operation = _admission_operation(
        daily,
        "durable-compile-replacement",
        quote,
        title="Durable Compile Admission Replacement",
        summary="A distinct replacement proposal is review-only.",
        body=body,
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(operation),
        False,
    )

    assert result["ok"] is True
    assert existing.read_bytes() == before
    assert b"status: superseded" not in existing.read_bytes()
    assert (root / "knowledge" / "notes" / "durable-compile-replacement.md").exists()


def test_execute_plan_defensively_refuses_changed_target_state(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Defensive replay checks preserve accepted action semantics."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-19.md",
        [_block("12:00:00", quote)],
    )
    existing = root / "knowledge" / "notes" / "appeared-after-admission.md"
    _write_note(existing, "Appeared Later", "A concurrent page appeared.")
    before = existing.read_bytes()
    create_plan = {
        "operations": [
            _admission_operation(daily, "appeared-after-admission", quote)
        ],
        "audit": {"verified": 1, "dedup": 3},
    }
    update_plan = {
        "operations": [
            _admission_operation(
                daily,
                "disappeared-after-admission",
                quote,
                action="update",
            )
        ],
        "audit": {"verified": 1, "dedup": 0},
    }

    with pytest.raises(FileExistsError, match="create target"):
        compile_memory._execute_plan(create_plan, [daily], False)
    with pytest.raises(FileNotFoundError, match="update target"):
        compile_memory._execute_plan(update_plan, [daily], False)

    assert existing.read_bytes() == before
    assert not (root / "knowledge" / "notes" / "disappeared-after-admission.md").exists()


def test_execute_plan_treats_lexical_symlink_as_existing(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Defensive execution must not treat a dangling link as absent."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-09-19-lexical.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "lexical-execute-target.md"
    plan = {
        "operations": [
            _admission_operation(daily, "lexical-execute-target", quote)
        ],
        "audit": {"verified": 1, "dedup": 3},
    }
    real_lstat = Path.lstat

    class SymlinkMetadata:
        st_mode = stat.S_IFLNK | 0o777
        st_file_attributes = 0

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path, *args, **kwargs: SymlinkMetadata()
        if path == target
        else real_lstat(path, *args, **kwargs),
    )

    with pytest.raises(FileExistsError, match="compile target"):
        compile_memory._execute_plan(plan, [daily], True)


@pytest.mark.parametrize(
    "malformed_plan",
    [
        [],
        {"operations": {}},
        {"operations": [None]},
        {"operations": [{"action": "create"}]},
    ],
)
def test_malformed_provider_plan_is_durable_apply_error(
    compile_env, monkeypatch, malformed_plan
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-27.md",
        [_block("12:00:00", "strict plan evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    payload = {"request": request, "response": json.dumps(malformed_plan)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])

    assert compile_memory.main() != 0
    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert "invalid provider plan" in state["last_compile_sdk_error"]["error"]
    assert "stage=validate" in (
        state_root / "logs" / "compile-sdk-last.log"
    ).read_text(encoding="utf-8")


def test_direct_retry_after_later_batch_failure_does_not_duplicate_write(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    first_evidence = "first evidence " + "a" * 14_000
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-28.md",
        [
            _block("12:00:00", first_evidence),
            _block("12:01:00", "second evidence " + "b" * 14_000),
        ],
    )
    monkeypatch.setenv("MEMORY_COMPILE_PROMPT_CHAR_BUDGET", "24000")
    second_attempts = 0

    def provider(prompt, _system_prompt, max_tokens=0):
        nonlocal second_attempts
        if "first evidence" in prompt:
            return json.dumps(
                {
                    "operations": [
                        {
                            "action": "create",
                            "category": "patterns",
                            "slug": "direct-once",
                            "title": "Direct Once",
                            "summary": "Written once.",
                            "body_section": "Lesson",
                            "body_markdown": _valid_body("DURABLE_ONCE"),
                            "evidence": [
                                {
                                    "daily_date": daily.stem,
                                    "timestamp": "12:00:00",
                                    "quoted_text": f"Rule: {first_evidence}",
                                    "claim": "first batch",
                                }
                            ],
                            "related": [],
                        }
                    ],
                    "audit": {},
                }
            )
        second_attempts += 1
        if second_attempts == 1:
            return '{"operations": [null]}'
        return '{"operations": [], "audit": {}}'

    import llm_client

    monkeypatch.setattr(llm_client, "call_llm", provider)

    assert not compile_memory._compile_succeeded(
        compile_memory.run_compile([daily], False)[1]
    )
    assert compile_memory._compile_succeeded(
        compile_memory.run_compile([daily], False)[1]
    )

    page = root / "knowledge" / "notes" / "direct-once.md"
    assert page.read_text(encoding="utf-8").count("DURABLE_ONCE") == 1
    assert state["compiled_daily_hashes"][daily.name] == (
        compile_memory._daily_snapshot_hash(daily)
    )


def test_source_append_during_apply_rejects_old_response_without_duplicate(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-29.md",
        [_block("12:00:00", "transaction evidence")],
    )
    page = root / "knowledge" / "notes" / "transaction-page.md"
    _write_note(page, "Original", "Original transaction page.")
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = json.dumps(
        {
            "operations": [
                {
                    "action": "update",
                    "category": "patterns",
                    "slug": "transaction-page",
                    "title": "Transaction Page",
                    "summary": "Transactional update.",
                    "body_section": "Lesson",
                    "body_markdown": "MUST_ROLL_BACK",
                    "evidence": [
                        {
                            "daily_date": daily.stem,
                            "timestamp": "12:00:00",
                            "quoted_text": "Rule: transaction evidence",
                            "claim": "transaction race",
                        }
                    ],
                    "related": [],
                }
            ],
            "audit": {},
        }
    )
    real_conditional_write = compile_memory.conditional_atomic_write
    raced = False

    def append_after_note_write(path, reservation, **kwargs):
        nonlocal raced
        real_conditional_write(path, reservation, **kwargs)
        if Path(path) == page and not raced:
            raced = True
            daily.write_text(
                daily.read_text(encoding="utf-8")
                + "\n"
                + _block("12:01:00", "arrived during apply")
                + "\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(
        compile_memory,
        "conditional_atomic_write",
        append_after_note_write,
    )

    result = compile_memory.apply_compile_batch(request, response, False)

    assert raced is True
    assert result["ok"] is True
    assert "llm-wiki-compile-op:" in page.read_text(encoding="utf-8")
    assert state["compiled_daily_hashes"][daily.name] == request["dailies"][0]["sha256"]
    assert compile_memory._daily_snapshot_hash(daily) != request["dailies"][0]["sha256"]

    retry_request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    retry = compile_memory.apply_compile_batch(retry_request, response, False)

    content = page.read_text(encoding="utf-8")
    assert retry["ok"] is False
    assert retry["status"] == "plan_rejected"
    assert content.count("MUST_ROLL_BACK") == 1
    assert state["compiled_daily_hashes"][daily.name] == request["dailies"][0][
        "sha256"
    ]
    assert state["compiled_daily_hashes"][daily.name] != (
        compile_memory._daily_snapshot_hash(daily)
    )


def test_append_after_transaction_commit_keeps_write_tracked_and_source_pending(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-30.md",
        [_block("12:00:00", "commit boundary evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    captured_hash = request["dailies"][0]["sha256"]
    response = json.dumps(
        {
            "operations": [
                {
                    "action": "create",
                    "category": "patterns",
                    "slug": "commit-boundary",
                    "title": "Commit Boundary",
                    "summary": "Tracks committed source version.",
                    "body_section": "Lesson",
                    "body_markdown": _valid_body("TRACKED_COMMIT"),
                    "evidence": [
                        {
                            "daily_date": daily.stem,
                            "timestamp": "12:00:00",
                            "quoted_text": "Rule: commit boundary evidence",
                            "claim": "commit boundary",
                        }
                    ],
                    "related": [],
                }
            ],
            "audit": {},
        }
    )
    real_execute = compile_memory._execute_plan

    def append_after_commit(plan, paths, dry_run, **kwargs):
        result = real_execute(plan, paths, dry_run, **kwargs)
        daily.write_text(
            daily.read_text(encoding="utf-8")
            + "\n"
            + _block("12:01:00", "new pending evidence")
            + "\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(
        compile_memory, "_execute_plan", append_after_commit
    )

    result = compile_memory.apply_compile_batch(request, response, False)

    assert result["ok"] is True
    assert state["compiled_daily_hashes"][daily.name] == captured_hash
    assert compile_memory._daily_snapshot_hash(daily) != captured_hash
    assert (root / "knowledge" / "notes" / "commit-boundary.md").exists()


def test_sdk_apply_waits_for_compile_lock_and_holds_it_through_progress(
    compile_env, monkeypatch
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-07-31.md",
        [_block("12:00:00", "serialized evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = '{"operations": [], "audit": {}}'
    real_update = compile_memory.update_state
    progress_saw_lock = False

    def inspect_progress_lock(mutator):
        nonlocal progress_saw_lock
        if getattr(mutator, "__name__", "") == "_complete":
            progress_saw_lock = (state_root / "run" / "compile.pid").exists()
        return real_update(mutator)

    monkeypatch.setattr(compile_memory, "update_state", inspect_progress_lock)
    finished = threading.Event()
    result = {}

    def apply_in_thread():
        result.update(compile_memory.apply_compile_batch(request, response, False))
        finished.set()

    with compile_memory._global_compile_lock():
        worker = threading.Thread(target=apply_in_thread)
        worker.start()
        time.sleep(0.05)
        assert not finished.is_set()
    worker.join(timeout=5)

    assert result["ok"] is True
    assert progress_saw_lock is True


def test_knowledge_publication_lock_is_reentrant_with_one_advisory_lock(
    tmp_path,
    monkeypatch,
):
    import memory_state

    events: list[object] = []

    @contextmanager
    def fake_advisory(path, **_kwargs):
        events.append(("enter", path))
        try:
            yield
        finally:
            events.append(("exit", path))

    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(memory_state, "advisory_file_lock", fake_advisory)

    with memory_state.knowledge_publication_lock():
        events.append("outer")
        with memory_state.knowledge_publication_lock():
            events.append("inner")

    expected = tmp_path / "run" / "knowledge-publication.lock"
    assert events == [
        ("enter", expected),
        "outer",
        "inner",
        ("exit", expected),
    ]


def test_global_compile_lock_precedes_shared_knowledge_publication_lock(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, _state = compile_env
    events: list[str] = []

    @contextmanager
    def fake_compile_lock(*_args, **_kwargs):
        events.append("compile-enter")
        try:
            yield
        finally:
            events.append("compile-exit")

    @contextmanager
    def fake_publication_lock(*_args, **_kwargs):
        events.append("publication-enter")
        try:
            yield
        finally:
            events.append("publication-exit")

    monkeypatch.setattr(compile_memory, "compile_file_lock", fake_compile_lock)
    monkeypatch.setattr(
        compile_memory,
        "knowledge_publication_lock",
        fake_publication_lock,
        raising=False,
    )

    with compile_memory._global_compile_lock():
        events.append("body")

    assert events == [
        "compile-enter",
        "publication-enter",
        "body",
        "publication-exit",
        "compile-exit",
    ]


def test_compile_rebuilds_index_in_process_under_reentrant_publication_lock(
    tmp_path,
    monkeypatch,
):
    import compile_memory
    import memory_state
    import rebuild_memory_index

    root = tmp_path / "vault"
    notes = root / "knowledge" / "notes"
    notes.mkdir(parents=True)
    state_root = tmp_path / "runtime"
    events: list[str] = []

    @contextmanager
    def observed_advisory_lock(_path, **kwargs):
        description = str(kwargs.get("description"))
        events.append(f"{description}-enter")
        try:
            yield
        finally:
            events.append(f"{description}-exit")

    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "advisory_file_lock", observed_advisory_lock)
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(rebuild_memory_index, "ROOT", root)
    monkeypatch.setattr(rebuild_memory_index, "memory", root / "knowledge")
    monkeypatch.setattr(rebuild_memory_index, "knowledge", notes)
    monkeypatch.setattr(
        rebuild_memory_index,
        "out",
        root / "knowledge" / "index.md",
    )

    with compile_memory._global_compile_lock():
        events.append("body")
        assert compile_memory.rebuild_index() is True

    assert rebuild_memory_index.out.exists()
    assert events == [
        "compile lock-enter",
        "knowledge publication lock-enter",
        "body",
        "knowledge publication lock-exit",
        "compile lock-exit",
    ]


def test_crash_after_note_commit_resumes_without_duplicate_append(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-01.md",
        [_block("12:00:00", "crash boundary evidence")],
    )
    page = root / "knowledge" / "notes" / "crash-idempotent.md"
    _write_note(page, "Existing", "Existing crash-idempotent page.")
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = json.dumps(
        {
            "operations": [
                {
                    "action": "update",
                    "category": "patterns",
                    "slug": "crash-idempotent",
                    "title": "Crash Idempotent",
                    "summary": "Resume safely.",
                    "body_section": "Lesson",
                    "body_markdown": "CRASH_BOUNDARY_BODY",
                    "evidence": [
                        {
                            "daily_date": daily.stem,
                            "timestamp": "12:00:00",
                            "quoted_text": "Rule: crash boundary evidence",
                            "claim": "crash boundary",
                        }
                    ],
                    "related": [],
                }
            ],
            "audit": {},
        }
    )
    real_update = compile_memory.update_state
    crashed = False

    def terminate_before_progress(mutator):
        nonlocal crashed
        if getattr(mutator, "__name__", "") == "_complete" and not crashed:
            crashed = True
            raise SystemExit("simulated termination")
        return real_update(mutator)

    monkeypatch.setattr(compile_memory, "update_state", terminate_before_progress)
    with pytest.raises(SystemExit, match="simulated termination"):
        compile_memory.apply_compile_batch(request, response, False)
    monkeypatch.setattr(compile_memory, "update_state", real_update)

    result = compile_memory.apply_compile_batch(request, response, False)

    content = page.read_text(encoding="utf-8")
    assert result["ok"] is True
    assert content.count("CRASH_BOUNDARY_BODY") == 1
    assert "llm-wiki-compile-op:" in content
    assert state["compiled_daily_hashes"][daily.name] == request["dailies"][0]["sha256"]


def test_crash_retry_uses_batch_operation_identity_not_changed_provider_text(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-02.md",
        [_block("12:00:00", "stable crash evidence")],
    )
    page = root / "knowledge" / "notes" / "stable-operation.md"
    _write_note(page, "Existing", "Existing stable-operation page.")
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    def response(body: str, claim: str) -> str:
        return json.dumps(
            {
                "operations": [
                    {
                        "action": "update",
                        "category": "patterns",
                        "slug": "stable-operation",
                        "title": "Stable Operation",
                        "summary": "Provider wording may change.",
                        "body_section": "Lesson",
                        "body_markdown": body,
                        "evidence": [
                            {
                                "daily_date": daily.stem,
                                "timestamp": "12:00:00",
                                "quoted_text": "Rule: stable crash evidence",
                                "claim": claim,
                            }
                        ],
                        "related": [],
                    }
                ],
                "audit": {},
            }
        )

    real_update = compile_memory.update_state
    crashed = False

    def terminate_before_progress(mutator):
        nonlocal crashed
        if getattr(mutator, "__name__", "") == "_complete" and not crashed:
            crashed = True
            raise SystemExit("simulated termination")
        return real_update(mutator)

    monkeypatch.setattr(compile_memory, "update_state", terminate_before_progress)
    with pytest.raises(SystemExit, match="simulated termination"):
        compile_memory.apply_compile_batch(
            request, response("ORIGINAL_PROVIDER_BODY", "original wording"), False
        )
    monkeypatch.setattr(compile_memory, "update_state", real_update)

    result = compile_memory.apply_compile_batch(
        request, response("CHANGED_PROVIDER_BODY", "changed wording"), False
    )

    content = page.read_text(encoding="utf-8")
    assert result["ok"] is True
    assert content.count("ORIGINAL_PROVIDER_BODY") == 1
    assert "CHANGED_PROVIDER_BODY" not in content


def test_accepted_journal_replays_original_target_and_content_after_crash(
    compile_env, monkeypatch
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-04.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    original = _operation(daily, "journal-original", "ORIGINAL_JOURNALED_BODY")
    changed = _operation(daily, "journal-changed", "CHANGED_REGENERATED_BODY")
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )
    real_update = compile_memory.update_state
    crashed = False

    def crash_before_progress(mutator):
        nonlocal crashed
        if getattr(mutator, "__name__", "") == "_complete" and not crashed:
            crashed = True
            raise SystemExit("crash after note")
        return real_update(mutator)

    monkeypatch.setattr(compile_memory, "update_state", crash_before_progress)
    with pytest.raises(SystemExit, match="crash after note"):
        compile_memory.apply_compile_batch(request, _response(original), False)
    monkeypatch.setattr(compile_memory, "update_state", real_update)

    result = compile_memory.apply_compile_batch(request, _response(changed), False)

    original_page = root / "knowledge" / "notes" / "journal-original.md"
    assert result["ok"] is True
    assert original_page.read_text(encoding="utf-8").count("ORIGINAL_JOURNALED_BODY") == 1
    assert not (root / "knowledge" / "notes" / "journal-changed.md").exists()
    journal = json.loads(
        (state_root / "run" / "compile-journal" / f"{request['batch_id']}.json")
        .read_text(encoding="utf-8")
    )
    assert journal["accepted"]["operations"][0]["slug"] == "journal-original"
    assert journal["accepted"]["response_sha256"] != compile_memory.hashlib.sha256(
        _response(changed).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("mutate", "error_text"),
    [
        (lambda operation: operation.update(category="unknown"), "category"),
        (lambda operation: operation.update(slug="../escape"), "slug"),
        (
            lambda operation: operation["evidence"][0].update(
                quoted_text="not exact evidence"
            ),
            "evidence",
        ),
    ],
)
def test_invalid_operation_rejects_whole_batch_before_first_mutation(
    compile_env, mutate, error_text
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-05.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    valid = _operation(daily, "must-not-write", "VALID_FIRST_BODY")
    invalid = _operation(daily, "invalid-second", "INVALID_SECOND_BODY")
    mutate(invalid)

    result = compile_memory.apply_compile_batch(
        request, _response(valid, invalid), False
    )

    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    assert error_text in result["error"]
    assert list((root / "knowledge" / "notes").glob("*.md")) == []
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()


def test_tampered_batch_id_cannot_escape_journal_directory(compile_env):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-11.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    request["batch_id"] = "../../escaped-journal"

    result = compile_memory.apply_compile_batch(
        request, _response(_operation(daily, "safe-target", "SAFE_BODY")), False
    )

    assert result["ok"] is False
    assert result["status"] == "stale"
    assert not (state_root / "run" / "escaped-journal.json").exists()
    assert not (root / "knowledge" / "notes" / "safe-target.md").exists()


def test_duplicate_manifest_key_fails_closed_before_journal_or_note(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Duplicate manifest keys cannot select compiler state."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-22-duplicate-manifest.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    manifest_path = (
        state_root
        / "run"
        / "compile-manifests"
        / f"{request['generation_id']}.json"
    )
    raw = manifest_path.read_text(encoding="utf-8")
    version = compile_memory.CURRENT_MANIFEST_VERSION
    manifest_path.write_text(
        raw.replace(
            f'"version": {version},',
            f'"version": {version},\n  "version": {version},',
            1,
        ),
        encoding="utf-8",
    )
    slug = "duplicate-manifest-target"

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )

    assert result["ok"] is False
    assert result["status"] == "manifest_error"
    assert not (root / "knowledge" / "notes" / f"{slug}.md").exists()
    assert not (state_root / "run" / "compile-journal").exists()
    assert daily.name not in state.get("compiled_daily_hashes", {})


def test_manifest_reader_accumulates_short_descriptor_reads(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-22-short-manifest-read.md",
        [_block("12:00:00", "Manifest reads may complete in short chunks.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    real_read = compile_memory.os.read

    def short_read(descriptor, size):
        return real_read(descriptor, min(size, 7))

    monkeypatch.setattr(compile_memory.os, "read", short_read)

    manifest = compile_memory._load_manifest(request["generation_id"])

    assert manifest["generation_id"] == request["generation_id"]


def test_duplicate_journal_key_fails_closed_before_update_resume(compile_env):
    case = _journaled_update_case(compile_env, "duplicate-journal")
    operation = json.loads(case["response"])["operations"][0]
    plan = {
        "operations": [operation],
        "audit": {
            field: 0
            for field in case["compile_memory"].COMPILE_AUDIT_FIELDS
        },
    }
    case["compile_memory"]._create_journal(
        case["request"],
        case["response"],
        plan,
    )
    raw = case["journal_path"].read_text(encoding="utf-8")
    version = case["compile_memory"].CURRENT_JOURNAL_VERSION
    case["journal_path"].write_text(
        raw.replace(
            f'"version": {version},',
            f'"version": {version},\n  "version": {version},',
            1,
        ),
        encoding="utf-8",
    )

    result = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    assert result["ok"] is False
    assert result["status"] == "journal_error"
    assert case["target"].read_bytes() == case["original"]
    assert case["daily"].name not in case["state"].get(
        "compiled_daily_hashes", {}
    )


def test_manifest_directory_replacement_aba_cannot_redirect_creation(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-22-manifest-dir-aba.md",
        [_block("12:00:00", "Manifest creation remains directory-bound.")],
    )
    manifest_dir = state_root / "run" / "compile-manifests"
    manifest_dir.mkdir(parents=True)
    external = state_root / "external-manifests"
    external.mkdir()
    marker = external / "external.json"
    marker.write_bytes(b"EXTERNAL JSON MUST REMAIN\n")
    before = marker.read_bytes()
    real_usage = compile_memory._active_manifest_usage
    swap_attempted = False
    swapped = False

    def usage_then_replace(*args, **kwargs):
        nonlocal swap_attempted, swapped
        result = real_usage(*args, **kwargs)
        if not swapped:
            swap_attempted = True
            manifest_dir.rmdir()
            _directory_link_or_skip(manifest_dir, external)
            swapped = True
        return result

    monkeypatch.setattr(
        compile_memory,
        "_active_manifest_usage",
        usage_then_replace,
    )

    with pytest.raises((OSError, compile_memory.CompilePreparationError)):
        compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )

    assert swap_attempted is True
    assert marker.read_bytes() == before
    assert list(external.iterdir()) == [marker]


def test_manifest_pruning_rejects_linked_directory_without_external_unlink(
    compile_env,
):
    compile_memory, _root, state_root, _state = compile_env
    external = state_root / "external-prune"
    external.mkdir(parents=True)
    external_json = external / f"{'a' * 64}.json"
    external_json.write_bytes(b"EXTERNAL JSON MUST REMAIN\n")
    before = external_json.read_bytes()
    manifest_dir = state_root / "run" / "compile-manifests"
    manifest_dir.parent.mkdir(parents=True, exist_ok=True)
    _directory_link_or_skip(manifest_dir, external)

    with pytest.raises((OSError, compile_memory.CompilePreparationError)):
        compile_memory._prune_completed_manifests()

    assert external_json.read_bytes() == before


def test_recomputed_manifest_hash_cannot_bless_inconsistent_derived_layout(
    compile_env, monkeypatch, capsys
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-12.md",
        [
            _block("12:00:00", "journal evidence " + "a" * 14_000),
            _block("12:01:00", "journal evidence " + "b" * 14_000),
        ],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    manifest_path = (
        state_root / "run" / "compile-manifests" / f"{request['generation_id']}.json"
    )
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampers = (
        lambda manifest: manifest["batches"].pop(0),
        lambda manifest: manifest["batches"].reverse(),
        lambda manifest: manifest["batches"][0].update(
            source_blocks=["substituted source block"]
        ),
    )
    for tamper in tampers:
        manifest = json.loads(json.dumps(original))
        tamper(manifest)
        manifest["manifest_sha256"] = compile_memory._manifest_digest(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        payload = {"request": request, "response": '{"operations": [], "audit": {}}'}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])

        assert compile_memory.main() != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert state["last_compile_sdk_error"]["stage"] == "manifest"
        assert daily.name not in state.get("compiled_daily_hashes", {})
        assert state["compile_generation_active"][daily.name]["generation_id"] == request[
            "generation_id"
        ]


def test_malformed_nested_manifest_is_durable_cli_error_without_traceback(
    compile_env, monkeypatch, capsys
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-21.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    manifest_path = (
        state_root / "run" / "compile-manifests" / f"{request['generation_id']}.json"
    )
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    malformed_values = (
        lambda manifest: manifest.update(batches=[None]),
        lambda manifest: manifest.update(daily=[]),
        lambda manifest: manifest.update(layout="wrong"),
        lambda manifest: manifest.update(unexpected=True),
        lambda manifest: manifest.update(batch_ids=["not-a-sha256"]),
        lambda manifest: manifest["batches"][0].update(batch_index=[]),
    )

    for mutate in malformed_values:
        manifest = json.loads(json.dumps(original))
        mutate(manifest)
        manifest["manifest_sha256"] = compile_memory._manifest_digest(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        payload = {"request": request, "response": '{"operations": [], "audit": {}}'}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])

        assert compile_memory.main() != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert state["last_compile_sdk_error"]["stage"] == "manifest"
        assert daily.name not in state.get("compiled_daily_hashes", {})

    manifest = json.loads(json.dumps(original))
    manifest["batches"] = [None]
    manifest["manifest_sha256"] = compile_memory._manifest_digest(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--prepare-sdk-request"])

    assert compile_memory.main() != 0
    assert "Traceback" not in capsys.readouterr().err
    assert state["last_compile_sdk_error"]["stage"] == "manifest"


def test_journal_mutable_state_tamper_fails_integrity_check(
    compile_env, monkeypatch
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-13.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)
    assert not compile_memory.apply_compile_batch(
        request, _response(_operation(daily, "mutable-integrity", "BODY")), False
    )["ok"]
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["operation_states"] = ["pending"]
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    result = compile_memory.apply_compile_batch(request, "changed", False)

    assert result["ok"] is False
    assert result["status"] == "journal_error"

    journal["operation_states"] = ["invalid-state"]
    compile_memory._write_journal(journal)
    result = compile_memory.apply_compile_batch(request, "changed", False)
    assert result["ok"] is False
    assert result["status"] == "journal_error"


def test_applied_journal_state_without_target_marker_replays_safely(
    compile_env, monkeypatch
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-14.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)
    response = _response(_operation(daily, "marker-required", "MARKER_BODY"))
    assert not compile_memory.apply_compile_batch(request, response, False)["ok"]
    target = root / "knowledge" / "notes" / "marker-required.md"
    target.unlink()
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    compile_memory._write_journal(journal)
    state.pop("compile_index_pending", None)
    state.get("compiled_daily_hashes", {}).pop(daily.name, None)
    state.get("compile_sdk_progress", {}).pop(daily.name, None)
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )

    result = compile_memory.apply_compile_batch(request, "changed", False)

    assert result["ok"] is True
    content = target.read_text(encoding="utf-8")
    assert content.count("MARKER_BODY") == 1
    assert "llm-wiki-compile-op:" in content


def test_operation_durable_effect_requires_marker_and_fingerprint_with_a_bound(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, _state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-14-bounded.md",
        [_block("12:00:00", "bounded replay evidence")],
    )
    operation = _operation(daily, "bounded-replay-marker", "BOUNDED_REPLAY_BODY")
    journal = {
        "batch_id": "a" * 64,
        "accepted": {"operations": [operation]},
    }
    target = root / "knowledge" / "notes" / "bounded-replay-marker.md"
    marker = compile_memory._operation_marker(
        {"batch_id": journal["batch_id"]},
        0,
        operation,
    )
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target.write_text(f"# Generated target\n\n{marker}\n", encoding="utf-8")
    real_open = Path.open
    read_sizes: list[int] = []

    class BoundedHandle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            read_sizes.append(size)
            assert size > 0, "durable-effect reconciliation read without a bound"
            return self.handle.read(size)

        def write(self, value):
            return self.handle.write(value)

    def bounded_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return BoundedHandle(handle) if path == target else handle

    monkeypatch.setattr(Path, "open", bounded_open)

    assert compile_memory._operation_has_durable_effect(journal, 0) is False
    target.write_text(
        f"# Generated target\n\n{marker}\n{fingerprint}\n",
        encoding="utf-8",
    )
    assert compile_memory._operation_has_durable_effect(journal, 0) is True
    assert read_sizes and all(size > 0 for size in read_sizes)


def test_operation_durable_effect_rejects_modeled_reparse_marker(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, _state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-14-reparse.md",
        [_block("12:00:00", "reparse replay evidence")],
    )
    operation = _operation(daily, "reparse-replay-marker", "REPARSE_REPLAY_BODY")
    journal = {
        "batch_id": "b" * 64,
        "accepted": {"operations": [operation]},
    }
    target = root / "knowledge" / "notes" / "reparse-replay-marker.md"
    marker = compile_memory._operation_marker(
        {"batch_id": journal["batch_id"]},
        0,
        operation,
    )
    target.write_text(f"# Modeled reparse target\n\n{marker}\n", encoding="utf-8")
    real_lstat = Path.lstat

    class ReparseMetadata:
        def __init__(self, metadata):
            self.metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            self.st_file_attributes = 0x400

        def __getattr__(self, name):
            return getattr(self.metadata, name)

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path, *args, **kwargs: ReparseMetadata(
            real_lstat(path, *args, **kwargs)
        )
        if path == target
        else real_lstat(path, *args, **kwargs),
    )

    assert compile_memory._operation_has_durable_effect(journal, 0) is False


def test_operation_durable_effect_rejects_real_symlink_marker(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-14-symlink.md",
        [_block("12:00:00", "symlink replay evidence")],
    )
    operation = _operation(daily, "symlink-replay-marker", "SYMLINK_REPLAY_BODY")
    journal = {
        "batch_id": "c" * 64,
        "accepted": {"operations": [operation]},
    }
    target = root / "knowledge" / "notes" / "symlink-replay-marker.md"
    outside = root.parent / "outside-replay-marker.md"
    marker = compile_memory._operation_marker(
        {"batch_id": journal["batch_id"]},
        0,
        operation,
    )
    outside.write_text(f"# External target\n\n{marker}\n", encoding="utf-8")
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    assert compile_memory._operation_has_durable_effect(journal, 0) is False


def test_reconciliation_blocks_hash_publication_from_symlink_marker(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-14-reconcile.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = _response(
        _operation(daily, "reconcile-symlink-marker", "RECONCILE_REPLAY_BODY")
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)

    first = compile_memory.apply_compile_batch(request, response, False)

    assert first["ok"] is False
    assert first["status"] == "index_pending"
    target = root / "knowledge" / "notes" / "reconcile-symlink-marker.md"
    generated = target.read_bytes()
    outside = root.parent / "outside-reconcile-marker.md"
    outside.write_bytes(generated)
    target.unlink()
    emulate_reparse = False
    try:
        target.symlink_to(outside)
    except OSError:
        target.write_bytes(generated)
        emulate_reparse = True

    if emulate_reparse:
        real_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, metadata):
                self.metadata = metadata
                self.st_mode = metadata.st_mode
                self.st_size = metadata.st_size
                self.st_file_attributes = 0x400

            def __getattr__(self, name):
                return getattr(self.metadata, name)

        monkeypatch.setattr(
            Path,
            "lstat",
            lambda path, *args, **kwargs: ReparseMetadata(
                real_lstat(path, *args, **kwargs)
            )
            if path == target
            else real_lstat(path, *args, **kwargs),
        )
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )

    result = compile_memory.apply_compile_batch(request, "changed", False)

    assert result["ok"] is False
    assert result["status"] == "apply_failed"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert "compile target" in result["error"]
    assert "compile_index_pending" not in state
    persisted = json.loads(
        (state_root / "run" / "compile-journal" / f"{request['batch_id']}.json")
        .read_text(encoding="utf-8")
    )
    assert persisted["operation_states"] == ["pending"]


def test_pending_index_resume_reapplies_deleted_note_before_hash_publication(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-14-resume-deleted.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = _response(
        _operation(daily, "resume-deleted-note", "RESUME_DELETED_BODY")
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)

    first = compile_memory.apply_compile_batch(request, response, False)

    assert first["status"] == "index_pending"
    target = root / "knowledge" / "notes" / "resume-deleted-note.md"
    target.unlink()
    note_existed_during_rebuild = []

    def rebuild_after_note_recovery():
        note_existed_during_rebuild.append(target.is_file())
        return _rebuild_test_index(compile_memory)

    monkeypatch.setattr(compile_memory, "rebuild_index", rebuild_after_note_recovery)

    result = compile_memory._resume_pending_index_if_any()

    assert result["ok"] is True
    assert note_existed_during_rebuild == [True]
    assert target.read_text(encoding="utf-8").count("RESUME_DELETED_BODY") == 1
    assert state["compiled_daily_hashes"][daily.name] == request["dailies"][0][
        "sha256"
    ]


def test_pending_index_resume_rejects_reparse_note_before_hash_publication(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-14-resume-reparse.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = _response(
        _operation(daily, "resume-reparse-note", "RESUME_REPARSE_BODY")
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)

    first = compile_memory.apply_compile_batch(request, response, False)

    assert first["status"] == "index_pending"
    target = root / "knowledge" / "notes" / "resume-reparse-note.md"
    generated = target.read_bytes()
    target.unlink()
    target.write_bytes(generated)
    real_lstat = Path.lstat

    class ReparseMetadata:
        def __init__(self, metadata):
            self.metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            self.st_file_attributes = 0x400

        def __getattr__(self, name):
            return getattr(self.metadata, name)

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path, *args, **kwargs: ReparseMetadata(
            real_lstat(path, *args, **kwargs)
        )
        if path == target
        else real_lstat(path, *args, **kwargs),
    )
    rebuilds = []
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: rebuilds.append(1) or _rebuild_test_index(compile_memory),
    )

    result = compile_memory._resume_pending_index_if_any()

    assert result["ok"] is False
    assert result["status"] == "apply_failed"
    assert rebuilds == []
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert state["compile_index_pending"]["batch_id"] == request["batch_id"]


def test_provider_delete_rejects_whole_batch_without_note_mutation(compile_env):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-17.md",
        [_block("12:00:00", "journal evidence")],
    )
    target = root / "knowledge" / "notes" / "delete-me.md"
    target.write_text("obsolete", encoding="utf-8")
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _operation(daily, "delete-me", "Delete obsolete page")
    operation["action"] = "delete"
    result = compile_memory.apply_compile_batch(request, _response(operation), False)

    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    assert "action" in result["error"]
    assert target.read_text(encoding="utf-8") == "obsolete"
    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert not list((state_root / "run" / "compile-journal").glob("*.json"))


def test_unexpected_audit_field_rejects_whole_batch(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    raw = json.dumps(
        {
            "operations": [_operation(daily, "audit-reject", "BODY")],
            "audit": {"verified": 1, "unexpected": 1},
        }
    )

    result = compile_memory.apply_compile_batch(request, raw, False)

    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    assert "audit" in result["error"]
    assert not (root / "knowledge" / "notes" / "audit-reject.md").exists()


@pytest.mark.parametrize(
    "wrap",
    (
        lambda raw: f"Provider preface\n{raw}",
        lambda raw: f"```json\n{raw}\n```",
        lambda raw: f"{raw}\nProvider epilogue",
        lambda raw: f"{raw}\n{raw}",
    ),
    ids=("prose-before", "markdown-fence", "trailing-text", "multiple-values"),
)
def test_provider_requires_exactly_one_unfenced_json_document(
    compile_env,
    wrap,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-23-provider-document.md",
        [_block("12:00:00", "Provider output is one strict JSON document.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        wrap(_response()),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert retry["batch_id"] == request["batch_id"]


def test_provider_allows_json_surrounded_only_by_whitespace(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-23-provider-whitespace.md",
        [_block("12:00:00", "JSON whitespace remains valid transport framing.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        f" \t\r\n{_response()}\n\t ",
        False,
    )

    assert result["ok"] is True


@pytest.mark.parametrize(
    "raw",
    (
        'Provider preface\n{"operations": [',
        '```json\n{"operations": [], "audit": {\n```',
    ),
)
def test_truncated_provider_json_is_rejected_without_traceback(
    compile_env,
    raw,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15-truncated-json.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(request, raw, False)

    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    assert "JSON" in result["error"]
    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()


def test_deep_provider_json_is_rejected_without_traceback(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15-deep-json.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    nesting = 2_000
    raw = '{"operations":' + "[" * nesting + "0" + "]" * nesting + "}"

    result = compile_memory.apply_compile_batch(request, raw, False)

    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    assert state["last_compile_sdk_error"]["stage"] == "validate"


@pytest.mark.parametrize("transport", ("direct", "sdk"))
@pytest.mark.parametrize(
    "hostile_kind",
    ("huge-integer", "deep-array", "deep-object", "nonfinite"),
)
def test_provider_json_resource_failures_are_durable_rejections(
    compile_env,
    monkeypatch,
    capsys,
    transport,
    hostile_kind,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15-hostile-json.md",
        [_block("12:00:00", "hostile provider evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    if hostile_kind == "huge-integer":
        raw = '{"operations":[],"audit":{"verified":' + "9" * 5_000 + "}}"
    elif hostile_kind == "deep-array":
        depth = compile_memory.MAX_PROVIDER_JSON_DEPTH + 2
        raw = '{"operations":' + "[" * depth + "0" + "]" * depth + "}"
    elif hostile_kind == "deep-object":
        depth = compile_memory.MAX_PROVIDER_JSON_DEPTH + 2
        raw = (
            '{"operations":[],"audit":{},"ignored":'
            + '{"child":' * depth
            + "0"
            + "}" * depth
            + "}"
        )
    else:
        raw = '{"operations":[],"audit":{"verified":NaN}}'

    if transport == "direct":
        result = compile_memory.apply_compile_batch(request, raw, False)
        assert result["ok"] is False
        assert result["status"] == "plan_rejected"
    else:
        payload = {"request": request, "response": raw}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])
        assert compile_memory.main() != 0
        assert "Traceback" not in capsys.readouterr().err

    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()
    assert list((root / "knowledge" / "notes").glob("*.md")) == []


@pytest.mark.parametrize("transport", ("direct", "sdk"))
@pytest.mark.parametrize(
    "location",
    (
        "root-key",
        "operation-key",
        "action",
        "category",
        "slug",
        "title",
        "summary",
        "body_section",
        "body_markdown",
        "evidence-key",
        "daily_date",
        "timestamp",
        "quoted_text",
        "claim",
        "related",
        "audit-key",
        "audit-value",
        "nested-ignored-key",
        "nested-ignored-value",
    ),
)
def test_provider_json_rejects_lone_surrogates_across_entire_graph(
    compile_env,
    monkeypatch,
    capsys,
    transport,
    location,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Surrogate validation covers the entire decoded provider graph."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15-surrogate.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(daily, "surrogate-target", quote)
    plan = {"operations": [operation], "audit": {}}
    surrogate = "\ud800"
    if location == "root-key":
        plan[surrogate] = "ignored"
    elif location == "operation-key":
        operation[surrogate] = "ignored"
    elif location in {
        "action",
        "category",
        "slug",
        "title",
        "summary",
        "body_section",
        "body_markdown",
    }:
        operation[location] = surrogate
    elif location == "evidence-key":
        operation["evidence"][0][surrogate] = "ignored"
    elif location in {"daily_date", "timestamp", "quoted_text", "claim"}:
        operation["evidence"][0][location] = surrogate
    elif location == "related":
        operation["related"] = [surrogate]
    elif location == "audit-key":
        plan["audit"][surrogate] = 0
    elif location == "audit-value":
        plan["audit"]["verified"] = surrogate
    elif location == "nested-ignored-key":
        operation["ignored"] = {"nested": [{surrogate: "safe"}]}
    else:
        operation["ignored"] = {"nested": [{"safe": surrogate}]}
    raw = json.dumps(plan)

    if transport == "direct":
        result = compile_memory.apply_compile_batch(request, raw, False)
        assert result["ok"] is False
        assert result["status"] == "plan_rejected"
        assert "Unicode" in result["error"]
    else:
        payload = {"request": request, "response": raw}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])
        assert compile_memory.main() != 0
        assert "Traceback" not in capsys.readouterr().err

    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()
    assert not (root / "knowledge" / "notes" / "surrogate-target.md").exists()


@pytest.mark.parametrize("transport", ("sdk", "direct"))
@pytest.mark.parametrize(
    "encoded_value",
    (
        pytest.param("\ud800", id="literal-lone"),
        pytest.param("\ud800\udc00", id="literal-pair"),
        pytest.param(r"\ud800", id="escaped-lone"),
        pytest.param(r"\ud800\udc00", id="escaped-pair"),
    ),
)
def test_provider_json_rejects_every_literal_and_escaped_surrogate_unit(
    compile_env,
    transport,
    encoded_value,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15-all-surrogates.md",
        [_block("12:00:00", "surrogate rejection evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    raw = (
        '{"operations":[],"audit":{"verified":"'
        + encoded_value
        + '"}}'
    )

    if transport == "sdk":
        result = compile_memory.apply_compile_batch(request, raw, False)
        assert result["ok"] is False
        assert result["status"] == "plan_rejected"
        assert "Unicode" in result["error"]
    else:
        touched, report = compile_memory._apply_compile_response(
            request,
            raw,
            [daily],
            False,
        )
        assert touched == []
        assert "invalid provider plan" in report
        assert "Unicode" in report

    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()


@pytest.mark.parametrize("transport", ("sdk", "direct"))
def test_malformed_provider_preview_with_surrogate_is_durable_and_utf8_safe(
    compile_env,
    transport,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15-surrogate-preview.md",
        [_block("12:00:00", "malformed preview evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    raw = "not-json " + "\ud800"

    if transport == "sdk":
        result = compile_memory.apply_compile_batch(request, raw, False)
        assert result["ok"] is False
        assert result["status"] == "plan_rejected"
        diagnostic = result["error"]
    else:
        touched, diagnostic = compile_memory._apply_compile_response(
            request,
            raw,
            [daily],
            False,
        )
        assert touched == []
    diagnostic.encode("utf-8", errors="strict")
    state["last_compile_sdk_error"]["error"].encode("utf-8", errors="strict")
    assert "\\ud800" in diagnostic.lower()
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not list((root / "knowledge" / "notes").glob("*.md"))
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()


def test_record_sdk_failure_sanitizes_untrusted_unicode_and_controls(compile_env):
    compile_memory, _root, state_root, state = compile_env

    class HostileError:
        def __str__(self):
            return "bad\ud800\nforged-log-line\x00"

    compile_memory.record_sdk_failure("provider", HostileError(), "batch\udc00")

    entry = state["last_compile_sdk_error"]
    entry["error"].encode("utf-8", errors="strict")
    entry["batch_id"].encode("utf-8", errors="strict")
    assert "\\ud800" in entry["error"].lower()
    assert "\\x0a" in entry["error"].lower()
    assert "\\udc00" in entry["batch_id"].lower()
    log = (state_root / "logs" / "compile-sdk-last.log").read_text(
        encoding="utf-8",
        errors="strict",
    )
    assert len(log.splitlines()) == 1


@pytest.mark.parametrize("transport", ("direct", "sdk"))
@pytest.mark.parametrize("error_type", (MemoryError, OverflowError, UnicodeError))
def test_provider_json_parser_exceptions_never_escape_apply(
    compile_env,
    monkeypatch,
    capsys,
    transport,
    error_type,
):
    import json as stdlib_json

    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-15-parser-error.md",
        [_block("12:00:00", "parser exception evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    raw = '{"operations":[],"audit":{}}'

    class FailingJSON:
        JSONDecodeError = stdlib_json.JSONDecodeError

        @staticmethod
        def loads(value, *args, **kwargs):
            if value == raw:
                raise error_type("simulated provider parser failure")
            return stdlib_json.loads(value, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(stdlib_json, name)

    monkeypatch.setattr(compile_memory, "json", FailingJSON())

    if transport == "direct":
        result = compile_memory.apply_compile_batch(request, raw, False)
        assert result["ok"] is False
        assert result["status"] == "plan_rejected"
    else:
        payload = {"request": request, "response": raw}
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdlib_json.dumps(payload)))
        monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])
        assert compile_memory.main() != 0
        assert "Traceback" not in capsys.readouterr().err

    assert state["last_compile_sdk_error"]["stage"] == "validate"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()


def test_index_pending_is_not_cleared_when_atomic_index_publication_crashes(
    compile_env, monkeypatch
):
    import rebuild_memory_index

    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-16.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(rebuild_memory_index, "ROOT", root)
    monkeypatch.setattr(rebuild_memory_index, "memory", root / "knowledge")
    monkeypatch.setattr(
        rebuild_memory_index, "knowledge", root / "knowledge" / "notes"
    )
    monkeypatch.setattr(rebuild_memory_index, "out", root / "knowledge" / "index.md")
    monkeypatch.setattr(rebuild_memory_index, "SUBDIR_SECTIONS", {})

    def crash_atomic_write(_path, _content):
        raise SystemExit("power loss during index publication")

    monkeypatch.setattr(
        rebuild_memory_index, "atomic_write", crash_atomic_write, raising=False
    )
    monkeypatch.setattr(
        compile_memory, "rebuild_index", lambda: rebuild_memory_index.main() == 0
    )

    with pytest.raises(SystemExit, match="power loss during index publication"):
        compile_memory.apply_compile_batch(
            request, '{"operations": [], "audit": {}}', False
        )

    assert state["compile_index_pending"]["batch_id"] == request["batch_id"]


def test_crashed_generation_keeps_2_1_layout_across_1_1_1_rebudget_and_append(
    compile_env, monkeypatch
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-06.md",
        [
            _block("12:00:00", "journal evidence " + "a" * 14_000),
            _block("12:01:00", "journal evidence " + "b" * 14_000),
            _block("12:02:00", "journal evidence " + "c" * 14_000),
        ],
    )
    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=40_000
    )
    assert [len(batch) for batch in first["generation_layout"]] == [2, 1]
    original_hash = first["dailies"][0]["sha256"]
    original_generation = first["generation_id"]
    manifest_path = (
        state_root
        / "run"
        / "compile-manifests"
        / f"{original_generation}.json"
    )
    manifest_before = manifest_path.read_bytes()

    real_update_state = compile_memory.update_state
    crashed = False

    def crash_before_progress(mutator):
        nonlocal crashed
        if getattr(mutator, "__name__", "") == "_complete" and not crashed:
            crashed = True
            raise SystemExit("crash after note commit")
        return real_update_state(mutator)

    monkeypatch.setattr(compile_memory, "update_state", crash_before_progress)
    operation = _operation(daily, "generation-resume", "GENERATION_BODY")
    operation["evidence"][0]["quoted_text"] = (
        "Rule: journal evidence " + "a" * 14_000
    )
    with pytest.raises(SystemExit, match="crash after note commit"):
        compile_memory.apply_compile_batch(first, _response(operation), False)
    monkeypatch.setattr(compile_memory, "update_state", real_update_state)

    os.environ["MEMORY_COMPILE_PROMPT_CHAR_BUDGET"] = "24000"
    daily.write_text(
        daily.read_text(encoding="utf-8")
        + "\n"
        + _block("12:03:00", "appended generation evidence " + "d" * 2_000)
        + "\n",
        encoding="utf-8",
    )
    current_hash = compile_memory._daily_snapshot_hash(daily)

    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    assert retry["generation_id"] == original_generation
    assert retry["batch_id"] == first["batch_id"]
    assert [len(batch) for batch in retry["generation_layout"]] == [2, 1]
    assert compile_memory.apply_compile_batch(retry, "changed response", False)["ok"]

    second = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    assert second["generation_id"] == original_generation
    assert len(second["source_blocks"]) == 1
    assert compile_memory.apply_compile_batch(
        second, '{"operations": [], "audit": {}}', False
    )["daily_complete"]
    assert state["compiled_daily_hashes"][daily.name] == original_hash

    appended = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    assert appended["generation_id"] != original_generation
    assert appended["generation_layout"] == [appended["source_blocks"]]
    assert "appended generation evidence" in appended["prompt"]
    assert "journal evidence" not in "\n".join(appended["source_blocks"])
    final = compile_memory.apply_compile_batch(
        appended, '{"operations": [], "audit": {}}', False
    )

    assert final["daily_complete"] is True
    assert state["compiled_daily_hashes"][daily.name] == current_hash
    assert "compile_index_pending" not in state
    assert "compile_generation_active" not in state
    assert manifest_path.read_bytes() == manifest_before
    page = root / "knowledge" / "notes" / "generation-resume.md"
    assert page.read_text(encoding="utf-8").count("GENERATION_BODY") == 1


def test_malformed_sdk_container_types_fail_durably_without_traceback(
    compile_env, monkeypatch, capsys
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-18.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    payloads = (
        [],
        {"request": [], "response": '{"operations": [], "audit": {}}'},
        {"request": request, "response": []},
    )

    for payload in payloads:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])
        assert compile_memory.main() != 0
        assert "Traceback" not in capsys.readouterr().err
        assert state["last_compile_sdk_error"]["stage"] == "apply"


def test_index_failure_stays_pending_and_retry_does_not_reapply_notes(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-07.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    response = _response(_operation(daily, "index-resume", "INDEX_BODY"))
    rebuilds = iter([False, True])

    def rebuild_index():
        return (
            _rebuild_test_index(compile_memory)
            if next(rebuilds)
            else False
        )

    monkeypatch.setattr(compile_memory, "rebuild_index", rebuild_index)

    first = compile_memory.apply_compile_batch(request, response, False)

    page = root / "knowledge" / "notes" / "index-resume.md"
    assert first["ok"] is False
    assert first["status"] == "index_pending"
    assert state["compile_index_pending"]["batch_id"] == request["batch_id"]

    retry = compile_memory.apply_compile_batch(request, response, False)

    assert retry["ok"] is True
    assert retry["status"] == "applied"
    assert "compile_index_pending" not in state
    assert page.read_text(encoding="utf-8").count("INDEX_BODY") == 1


def test_successful_sdk_recovery_resets_error_only_after_final_index(compile_env, monkeypatch):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-22.md",
        [
            _block("12:00:00", "first recovery block " + "a" * 16_000),
            _block("12:01:00", "second recovery block " + "b" * 16_000),
        ],
    )
    state.update(
        {
            "last_compile_status": "error",
            "last_compile_error": "provider: prior Luna failure",
            "last_compile_sdk_error": {
                "stage": "provider",
                "error": "prior Luna failure",
            },
        }
    )
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )

    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    first_result = compile_memory.apply_compile_batch(
        first, '{"operations": [], "audit": {}}', False
    )

    assert first_result["ok"] is True
    assert first_result["daily_complete"] is False
    assert state["last_compile_status"] == "error"
    assert state["last_compile_error"] == "provider: prior Luna failure"
    assert state["last_compile_sdk_error"]["stage"] == "provider"

    second = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    final_result = compile_memory.apply_compile_batch(
        second, '{"operations": [], "audit": {}}', False
    )

    assert final_result["ok"] is True
    assert final_result["daily_complete"] is True
    assert state["last_compile_status"] == "ok"
    assert "last_compile_error" not in state
    assert "last_compile_sdk_error" not in state
    assert state["last_compile_finished_trigger"] == "sdk"


def test_sdk_completion_persists_normalized_audit(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-22-sdk-audit.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(audit={"stubs": 2, "rejected": 3}),
        False,
    )

    assert result["ok"] is True
    assert state["last_compile_audit"] == {
        "verified": 0,
        "dedup": 0,
        "stubs": 2,
        "contradictions": 0,
        "rejected": 3,
    }


def test_pending_index_recovery_uses_journal_audit_for_legacy_progress(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-22-legacy-audit.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)

    first = compile_memory.apply_compile_batch(
        request,
        _response(audit={"stubs": 2}),
        False,
    )

    assert first["status"] == "index_pending"
    state["compile_sdk_progress"][daily.name].pop("batch_audits")
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )

    result = compile_memory._resume_pending_index_if_any()

    assert result["ok"] is True
    assert state["last_compile_audit"]["stubs"] == 2


def test_direct_multibatch_uses_one_transactional_index_and_aggregates_audit(
    compile_env,
    monkeypatch,
):
    import llm_client

    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-22-direct-audit.md",
        [
            _block("12:00:00", "first batch " + "a" * 14_000),
            _block("12:01:00", "second batch " + "b" * 14_000),
        ],
    )
    provider_calls = []

    def provider(*_args, **_kwargs):
        provider_calls.append(1)
        return _response(audit={"stubs": 1})

    rebuilds = []
    monkeypatch.setattr(llm_client, "call_llm", provider)
    monkeypatch.setenv("MEMORY_COMPILE_PROMPT_CHAR_BUDGET", "24000")
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: rebuilds.append(1) or _rebuild_test_index(compile_memory),
    )
    monkeypatch.setattr(compile_memory, "append_log", lambda _entry: None)
    args = argparse.Namespace(
        trigger="manual",
        dry_run=False,
        file=None,
        all=False,
        sdk_paths=None,
    )
    monkeypatch.setattr(
        compile_memory,
        "select_dailies",
        lambda _args, _state: [daily],
    )

    assert compile_memory._run(args) == 0
    assert len(provider_calls) > 1
    assert rebuilds == [1]
    assert state["last_compile_audit"]["stubs"] == len(provider_calls)


@pytest.mark.parametrize("transport", ("sdk", "manual"))
def test_two_daily_compile_wave_aggregates_python_derived_audit_totals(
    compile_env,
    monkeypatch,
    transport,
):
    import llm_client

    compile_memory, root, _state_root, state = compile_env
    dailies = [
        _daily(
            root / "knowledge" / "daily" / f"2026-08-{24 + index}.md",
            [_block("12:00:00", f"wave evidence {index}")],
        )
        for index in range(2)
    ]

    def response_for(request):
        daily = next(
            path
            for path in dailies
            if request["dailies"][0]["path"].endswith(path.name)
        )
        index = dailies.index(daily)
        operation = _admission_operation(
            daily,
            f"wave-audit-{index}",
            f"wave evidence {index}",
            summary=f"Distinct wave audit summary {index}.",
        )
        return _response(operation, audit={"stubs": index + 1})

    if transport == "sdk":
        while True:
            request = compile_memory.prepare_compile_request(
                dailies,
                compile_memory.load_state(),
                prompt_char_budget=30_000,
            )
            if not request["pending"]:
                break
            result = compile_memory.apply_compile_batch(
                request,
                response_for(request),
                False,
            )
            assert result["ok"] is True
    else:
        def provider(prompt, _system_prompt, max_tokens=0):
            del max_tokens
            daily = dailies[0] if "wave evidence 0" in prompt else dailies[1]
            request = {
                "dailies": [{"path": f"knowledge/daily/{daily.name}"}],
            }
            return response_for(request)

        monkeypatch.setattr(llm_client, "call_llm", provider)
        _touched, report = compile_memory.run_compile(dailies, False)
        assert compile_memory.parse_compile_audit(report) == {
            "verified": 2,
            "dedup": 6,
            "stubs": 3,
            "contradictions": 0,
            "rejected": 0,
        }

    assert state["last_compile_audit"] == {
        "verified": 2,
        "dedup": 6,
        "stubs": 3,
        "contradictions": 0,
        "rejected": 0,
    }


def test_two_daily_sdk_audit_wave_resumes_index_without_double_count(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    dailies = [
        _daily(
            root / "knowledge" / "daily" / f"2026-08-{26 + index}.md",
            [_block("12:00:00", f"resume wave evidence {index}")],
        )
        for index in range(2)
    ]
    first = compile_memory.prepare_compile_request(
        dailies, state, prompt_char_budget=30_000
    )
    wave_id = state["compile_sdk_wave"]["wave_id"]
    assert compile_memory.apply_compile_batch(
        first,
        _response(audit={"stubs": 1}),
        False,
    )["ok"]
    second = compile_memory.prepare_compile_request(
        dailies, state, prompt_char_budget=30_000
    )
    assert state["compile_sdk_wave"]["wave_id"] == wave_id
    rebuilds = iter([False, True])

    def rebuild_wave_index():
        return next(rebuilds) and _rebuild_test_index(compile_memory)

    monkeypatch.setattr(compile_memory, "rebuild_index", rebuild_wave_index)

    failed = compile_memory.apply_compile_batch(
        second,
        _response(audit={"stubs": 2}),
        False,
    )

    assert failed["status"] == "index_pending"
    resumed = compile_memory._resume_pending_index_if_any()
    assert resumed["ok"] is True
    assert state["last_compile_audit"] == {
        "verified": 0,
        "dedup": 0,
        "stubs": 3,
        "contradictions": 0,
        "rejected": 0,
    }


def test_stale_prepare_cannot_overwrite_persisted_audit_wave(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    dailies = [
        _daily(
            root / "knowledge" / "daily" / f"2026-09-{20 + index}.md",
            [_block("12:00:00", f"prepare race evidence {index}")],
        )
        for index in range(2)
    ]
    stale_pre_wave = compile_memory.load_state()
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )
    first = compile_memory.prepare_compile_request(
        dailies,
        state,
        prompt_char_budget=30_000,
    )
    assert compile_memory.apply_compile_batch(
        first,
        _response(audit={"stubs": 1}),
        False,
    )["ok"]
    first_daily = Path(first["dailies"][0]["path"]).name
    persisted_wave = compile_memory.load_state()["compile_sdk_wave"]
    assert persisted_wave["daily_audits"][first_daily]["stubs"] == 1

    second = compile_memory.prepare_compile_request(
        dailies,
        stale_pre_wave,
        prompt_char_budget=30_000,
    )

    assert Path(second["dailies"][0]["path"]).name != first_daily
    surviving_wave = compile_memory.load_state()["compile_sdk_wave"]
    assert surviving_wave["completed"][first_daily] == persisted_wave["expected"][first_daily]
    assert surviving_wave["daily_audits"][first_daily]["stubs"] == 1
    assert compile_memory.apply_compile_batch(
        second,
        _response(audit={"stubs": 2}),
        False,
    )["ok"]
    assert state["last_compile_audit"]["stubs"] == 3
    assert compile_memory.prepare_compile_request([], stale_pre_wave) == {
        "pending": False
    }
    assert "compile_sdk_wave" not in state


def test_completed_sdk_audit_wave_cannot_leak_into_next_run(compile_env):
    compile_memory, root, _state_root, state = compile_env
    first_daily = _daily(
        root / "knowledge" / "daily" / "2026-08-28.md",
        [_block("12:00:00", "first isolated wave")],
    )
    first = compile_memory.prepare_compile_request(
        [first_daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        first,
        _response(audit={"stubs": 7}),
        False,
    )["ok"]
    assert state["compile_sdk_wave"]["status"] == "complete"

    second_daily = _daily(
        root / "knowledge" / "daily" / "2026-08-29.md",
        [_block("12:00:00", "second isolated wave")],
    )
    second = compile_memory.prepare_compile_request(
        [second_daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        second,
        _response(audit={"stubs": 2}),
        False,
    )["ok"]

    assert state["last_compile_audit"]["stubs"] == 2
    assert compile_memory.prepare_compile_request([], state) == {"pending": False}
    assert "compile_sdk_wave" not in state


def test_manual_resumed_wave_retains_canonical_aggregate_and_resets(
    compile_env,
    monkeypatch,
):
    import llm_client

    compile_memory, root, _state_root, state = compile_env
    dailies = [
        _daily(
            root / "knowledge" / "daily" / f"2026-08-{30 + index}.md",
            [_block("12:00:00", f"manual resumed wave {index}")],
        )
        for index in range(2)
    ]
    first = compile_memory.prepare_compile_request(
        dailies,
        state,
        prompt_char_budget=30_000,
    )
    assert compile_memory.apply_compile_batch(
        first,
        _response(audit={"stubs": 1}),
        False,
    )["ok"]
    assert state["compile_sdk_wave"]["status"] == "active"

    responses = iter(
        (
            _response(audit={"stubs": 2}),
            _response(audit={"stubs": 4}),
        )
    )
    monkeypatch.setattr(llm_client, "call_llm", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(compile_memory, "append_log", lambda _entry: None)
    selected = [dailies[1]]
    monkeypatch.setattr(
        compile_memory,
        "select_dailies",
        lambda _args, _state: list(selected),
    )
    args = argparse.Namespace(
        trigger="manual",
        dry_run=False,
        file=None,
        all=False,
        sdk_paths=None,
    )

    assert compile_memory._run(args) == 0
    assert "compile_sdk_wave" not in state
    assert state["last_compile_audit"]["stubs"] == 3

    next_daily = _daily(
        root / "knowledge" / "daily" / "2026-09-01.md",
        [_block("12:00:00", "fresh manual wave")],
    )
    selected[:] = [next_daily]

    assert compile_memory._run(args) == 0
    assert "compile_sdk_wave" not in state
    assert state["last_compile_audit"]["stubs"] == 4


def test_sdk_health_changes_only_after_successful_final_index(compile_env, monkeypatch):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-23.md",
        [
            _block("12:00:00", "first health block " + "a" * 16_000),
            _block("12:01:00", "second health block " + "b" * 16_000),
        ],
    )
    old_compile_at = "2000-01-01T00:00:00"
    state.update(
        {
            "last_compile_at": old_compile_at,
            "last_index_rebuild_ok": False,
        }
    )
    rebuilds = iter([False, True])

    def rebuild_health_index():
        return next(rebuilds) and _rebuild_test_index(compile_memory)

    monkeypatch.setattr(compile_memory, "rebuild_index", rebuild_health_index)

    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    first_result = compile_memory.apply_compile_batch(
        first, '{"operations": [], "audit": {}}', False
    )

    assert first_result["daily_complete"] is False
    assert state["last_compile_at"] == old_compile_at
    assert state["last_index_rebuild_ok"] is False

    final = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    failed_index = compile_memory.apply_compile_batch(
        final, '{"operations": [], "audit": {}}', False
    )

    assert failed_index["status"] == "index_pending"
    assert state["last_compile_at"] == old_compile_at
    assert state["last_index_rebuild_ok"] is False

    successful_retry = compile_memory.apply_compile_batch(
        final, '{"operations": [], "audit": {}}', False
    )

    assert successful_retry["daily_complete"] is True
    assert state["last_compile_at"] == state["last_compile_finished_at"]
    assert state["last_index_rebuild_ok"] is True


def test_sdk_cli_services_final_index_once_inside_journal_transaction(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-09.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    payload = {"request": request, "response": '{"operations": [], "audit": {}}'}
    rebuilds = []
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: rebuilds.append(1) or _rebuild_test_index(compile_memory),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--apply-sdk-response"])

    assert compile_memory.main() == 0
    assert rebuilds == [1]


def test_prepare_cli_resumes_index_pending_without_provider_request(
    compile_env, monkeypatch, capsys
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-10.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)
    assert not compile_memory.apply_compile_batch(
        request, '{"operations": [], "audit": {}}', False
    )["ok"]
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )
    monkeypatch.setattr(sys, "argv", ["compile_memory.py", "--prepare-sdk-request"])

    assert compile_memory.main() == 0

    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert output == {"pending": False}
    assert "compile_index_pending" not in state


def test_index_retry_rejects_journal_batch_ids_outside_active_manifest(
    compile_env, monkeypatch
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-20.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)
    assert not compile_memory.apply_compile_batch(
        request, '{"operations": [], "audit": {}}', False
    )["ok"]
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["accepted"]["batch_ids"] = ["f" * 64]
    journal["accepted_sha256"] = compile_memory._canonical_digest(journal["accepted"])
    compile_memory._write_journal(journal)
    rebuilds = []
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: rebuilds.append(1) or _rebuild_test_index(compile_memory),
    )

    result = compile_memory._resume_pending_index_if_any()

    assert result["ok"] is False
    assert result["status"] == "journal_error"
    assert rebuilds == []
    assert state["compile_index_pending"]["batch_id"] == request["batch_id"]


def test_sdk_lock_open_error_is_durable_failure(compile_env, monkeypatch):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-08.md",
        [_block("12:00:00", "journal evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    @contextmanager
    def denied_lock(*_args, **_kwargs):
        raise PermissionError("sharing violation")
        yield

    monkeypatch.setattr(compile_memory, "_global_compile_lock", denied_lock)

    result = compile_memory.apply_compile_batch(
        request, '{"operations": [], "audit": {}}', False
    )

    assert result["ok"] is False
    assert result["status"] == "lock_error"
    assert state["last_compile_sdk_error"]["stage"] == "lock"
    assert "sharing violation" in (
        state_root / "logs" / "compile-sdk-last.log"
    ).read_text(encoding="utf-8")


def test_direct_lock_open_error_returns_nonzero_without_traceback(
    compile_env, monkeypatch, capsys
):
    compile_memory, _root, state_root, state = compile_env

    @contextmanager
    def denied_lock(*_args, **_kwargs):
        raise PermissionError("direct sharing violation")
        yield

    monkeypatch.setattr(compile_memory, "_global_compile_lock", denied_lock)
    monkeypatch.setattr(sys, "argv", ["compile_memory.py"])

    result = compile_memory.main()

    captured = capsys.readouterr()
    assert result != 0
    assert "Traceback" not in captured.err
    assert "direct sharing violation" in captured.err
    assert state["last_compile_sdk_error"]["stage"] == "lock"
    assert "stage=lock" in (
        state_root / "logs" / "compile-sdk-last.log"
    ).read_text(encoding="utf-8")


def test_completed_journal_retention_is_bounded_without_pruning_pending(
    compile_env
):
    compile_memory, _root, state_root, _state = compile_env
    for index in range(205):
        compile_memory._write_journal(
            _synthetic_current_journal(
                compile_memory,
                f"{index:064x}",
                index,
                status="complete" if index else "index_pending",
            )
        )

    compile_memory._prune_completed_journals()

    journals = list((state_root / "run" / "compile-journal").glob("*.json"))
    assert len(journals) <= compile_memory.MAX_COMPLETED_JOURNALS + 1
    assert (state_root / "run" / "compile-journal" / f"{0:064x}.json").exists()


def test_completed_journal_pruning_moves_owned_bytes_to_retired_store(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, _state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    batch_id = "1" * 64
    compile_memory._write_journal(
        _synthetic_current_journal(compile_memory, batch_id)
    )
    active = state_root / "run" / "compile-journal" / f"{batch_id}.json"
    original = active.read_bytes()

    compile_memory._prune_completed_journals()

    assert not active.exists()
    [retired] = (state_root / "run" / "retired-journals").glob(
        f"{batch_id}.*.json"
    )
    assert retired.read_bytes() == original


@pytest.mark.parametrize("limit", ("count", "bytes"))
def test_retired_journal_quota_refuses_without_deleting_active_or_history(
    compile_env,
    monkeypatch,
    limit,
):
    compile_memory, _root, state_root, _state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    monkeypatch.setattr(compile_memory, "MAX_RETIRED_JOURNALS", 10, raising=False)
    monkeypatch.setattr(
        compile_memory,
        "MAX_RETIRED_JOURNAL_BYTES",
        1024 * 1024,
        raising=False,
    )
    active_dir = state_root / "run" / "compile-journal"
    first_id = "2" * 64
    first_path = active_dir / f"{first_id}.json"
    first_path.parent.mkdir(parents=True, exist_ok=True)
    first_bytes = _completed_journal_bytes(compile_memory, first_id, 1)
    first_path.write_bytes(first_bytes)
    if limit == "count":
        monkeypatch.setattr(compile_memory, "MAX_RETIRED_JOURNALS", 1, raising=False)
    compile_memory._prune_completed_journals()
    [first_retired] = (state_root / "run" / "retired-journals").glob(
        f"{first_id}.*.json"
    )

    second_id = "3" * 64
    second_path = active_dir / f"{second_id}.json"
    second_bytes = _completed_journal_bytes(compile_memory, second_id, 2)
    second_path.write_bytes(second_bytes)
    if limit == "bytes":
        monkeypatch.setattr(
            compile_memory,
            "MAX_RETIRED_JOURNAL_BYTES",
            len(first_bytes) + len(second_bytes) - 1,
            raising=False,
        )

    with pytest.raises(OSError, match=rf"retired journal {limit} limit"):
        compile_memory._prune_completed_journals()

    assert second_path.read_bytes() == second_bytes
    assert first_retired.read_bytes() == first_bytes


def test_completed_journal_pruning_preserves_active_generation_batches(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 2)
    active_batch = "a" * 64
    for sequence, batch_id in enumerate(
        (active_batch, "b" * 64, "c" * 64, "d" * 64)
    ):
        compile_memory._write_journal(
            _synthetic_current_journal(
                compile_memory,
                batch_id,
                sequence,
            )
        )
        time.sleep(0.002)
    state["compile_sdk_progress"] = {
        "active.md": {
            "generation_id": "e" * 64,
            "expected_batch_ids": [active_batch],
        }
    }

    compile_memory._prune_completed_journals()

    directory = state_root / "run" / "compile-journal"
    assert (directory / f"{active_batch}.json").is_file()
    assert len(list(directory.glob("*.json"))) <= 3


@pytest.mark.parametrize("malformed", ("duplicate", "oversized"))
def test_journal_pruning_rejects_non_strict_json_before_unlink(
    compile_env,
    monkeypatch,
    malformed,
):
    compile_memory, _root, state_root, _state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    monkeypatch.setattr(compile_memory, "MAX_COMPILE_JOURNAL_BYTES", 1024)
    directory = state_root / "run" / "compile-journal"
    valid_paths = []
    for index in range(2):
        journal = _synthetic_current_journal(
            compile_memory,
            f"{index:064x}",
            index,
        )
        compile_memory._write_journal(journal)
        valid_paths.append(directory / f"{index:064x}.json")
    malformed_path = directory / f"{'f' * 64}.json"
    if malformed == "duplicate":
        malformed_path.write_bytes(
            b'{"status":"complete","status":"pending"}'
        )
    else:
        malformed_path.write_bytes(
            b'{"status":"pending","padding":"'
            + b"x" * compile_memory.MAX_COMPILE_JOURNAL_BYTES
            + b'"}'
        )

    with pytest.raises(ValueError):
        compile_memory._prune_completed_journals()

    assert all(path.is_file() for path in valid_paths)
    assert malformed_path.is_file()


def test_journal_pruning_rejects_linked_directory_before_external_file_access(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, _state = compile_env
    external = state_root / "external-journals"
    external.mkdir(parents=True)
    for index in range(205):
        batch_id = f"{index:064x}"
        (external / f"{batch_id}.json").write_bytes(
            _completed_journal_bytes(compile_memory, batch_id, index)
        )
    before = {path.name: path.read_bytes() for path in external.iterdir()}
    journal_dir = state_root / "run" / "compile-journal"
    _directory_link_or_skip(journal_dir, external)
    synced = []
    real_sync = compile_memory.sync_file_strict

    def record_external_access(path):
        synced.append(Path(path))
        return real_sync(path)

    monkeypatch.setattr(compile_memory, "sync_file_strict", record_external_access)

    with pytest.raises((OSError, ValueError)):
        compile_memory._prune_completed_journals()

    assert synced == []
    assert {path.name: path.read_bytes() for path in external.iterdir()} == before


def test_journal_directory_replacement_cannot_redirect_pruning(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, _state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    batch_id = "a" * 64
    journal_dir = state_root / "run" / "compile-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / f"{batch_id}.json").write_bytes(
        _completed_journal_bytes(compile_memory, batch_id)
    )
    parked = state_root / "parked-journals"
    external = state_root / "external-journal-aba"
    external.mkdir()
    external_journal = external / f"{batch_id}.json"
    external_journal.write_bytes(
        _completed_journal_bytes(compile_memory, batch_id, 99)
    )
    before = external_journal.read_bytes()
    real_load_state = compile_memory.load_state
    swap_attempted = False

    def load_state_then_replace_directory():
        nonlocal swap_attempted
        state = real_load_state()
        if not swap_attempted:
            swap_attempted = True
            journal_dir.rename(parked)
            _directory_link_or_skip(journal_dir, external)
        return state

    monkeypatch.setattr(
        compile_memory,
        "load_state",
        load_state_then_replace_directory,
    )

    with pytest.raises((OSError, ValueError)):
        compile_memory._prune_completed_journals()

    assert swap_attempted is True
    assert external_journal.read_bytes() == before


def test_journal_replacement_at_retirement_is_restored_without_deletion(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, _state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    batch_id = "b" * 64
    journal_dir = state_root / "run" / "compile-journal"
    journal_dir.mkdir(parents=True)
    journal_path = journal_dir / f"{batch_id}.json"
    original = _completed_journal_bytes(compile_memory, batch_id)
    journal_path.write_bytes(original)
    checked = journal_dir / "checked-original.json"
    racer = journal_dir / "foreign-racer.tmp"
    foreign = b"FOREIGN JOURNAL MUST NOT BE DELETED\n"
    racer.write_bytes(foreign)
    real_retire_rename = compile_memory._rename_journal_child
    raced = False

    def replace_immediately_before_retirement(bound, source, destination):
        nonlocal raced
        if not raced and Path(source).name == journal_path.name:
            raced = True
            os.replace(journal_path, checked)
            os.replace(racer, journal_path)
        return real_retire_rename(bound, source, destination)

    monkeypatch.setattr(
        compile_memory,
        "_rename_journal_child",
        replace_immediately_before_retirement,
    )

    with pytest.raises((OSError, ValueError)):
        compile_memory._prune_completed_journals()

    assert raced is True
    assert journal_path.read_bytes() == foreign
    assert checked.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="no-replace rename is POSIX-specific")
def test_journal_retirement_never_overwrites_raced_quarantine(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, _state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    batch_id = "c" * 64
    journal_dir = state_root / "run" / "compile-journal"
    journal_dir.mkdir(parents=True)
    journal_path = journal_dir / f"{batch_id}.json"
    original = _completed_journal_bytes(compile_memory, batch_id)
    journal_path.write_bytes(original)
    foreign = b"FOREIGN QUARANTINE MUST NOT BE OVERWRITTEN\n"
    real_noreplace = getattr(compile_memory, "_rename_noreplace_posix", None)
    collision = None

    def create_collision_then_rename(dir_fd, source, destination):
        nonlocal collision
        collision = journal_dir / destination
        collision.write_bytes(foreign)
        if real_noreplace is None:
            raise AssertionError("native no-replace rename is unavailable")
        return real_noreplace(dir_fd, source, destination)

    monkeypatch.setattr(
        compile_memory,
        "_rename_noreplace_posix",
        create_collision_then_rename,
        raising=False,
    )

    with pytest.raises(OSError):
        compile_memory._prune_completed_journals()

    assert collision is not None
    assert collision.read_bytes() == foreign
    assert journal_path.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="directory descriptors are POSIX-specific")
def test_posix_noreplace_rename_moves_between_bound_directories_without_overwrite(
    tmp_path,
):
    import memory_state

    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "owned.json"
    source.write_bytes(b"OWNED SOURCE\n")
    collision = destination_dir / "collision.json"
    collision.write_bytes(b"FOREIGN DESTINATION\n")

    with memory_state.bind_atomic_writes_to_directory(source_dir) as source_bound:
        with memory_state.bind_atomic_writes_to_directory(
            destination_dir
        ) as destination_bound:
            memory_state._rename_noreplace_posix(
                source_bound.descriptor,
                source.name,
                "retired.json",
                destination_dir_fd=destination_bound.descriptor,
            )
            with pytest.raises(OSError):
                memory_state._rename_noreplace_posix(
                    destination_bound.descriptor,
                    "retired.json",
                    collision.name,
                    destination_dir_fd=destination_bound.descriptor,
                )

    assert not source.exists()
    assert (destination_dir / "retired.json").read_bytes() == b"OWNED SOURCE\n"
    assert collision.read_bytes() == b"FOREIGN DESTINATION\n"


def test_completed_manifest_retention_is_bounded_without_pruning_active(
    compile_env, monkeypatch
):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 2)
    old_daily = _daily(
        root / "knowledge" / "daily" / "2026-08-19.md",
        [_block("12:00:00", "old generation evidence")],
    )
    old_request = compile_memory.prepare_compile_request(
        [old_daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        old_request, '{"operations": [], "audit": {}}', False
    )["ok"]

    for index in range(4):
        other = _daily(
            root / "knowledge" / "daily" / f"2026-08-{22 + index}.md",
            [_block("12:00:00", f"other generation evidence {index}")],
        )
        request = compile_memory.prepare_compile_request(
            [other], state, prompt_char_budget=30_000
        )
        assert compile_memory.apply_compile_batch(
            request, '{"operations": [], "audit": {}}', False
        )["ok"]

    directory = state_root / "run" / "compile-manifests"
    assert len(list(directory.glob("*.json"))) <= 2
    assert not (directory / f"{old_request['generation_id']}.json").exists()

    old_daily.write_text(
        old_daily.read_text(encoding="utf-8")
        + "\n"
        + _block("12:01:00", "new suffix evidence")
        + "\n",
        encoding="utf-8",
    )
    appended = compile_memory.prepare_compile_request(
        [old_daily], state, prompt_char_budget=30_000
    )
    assert len(appended["source_blocks"]) == 1
    assert "new suffix evidence" in appended["source_blocks"][0]
    assert "old generation evidence" not in appended["prompt"]

    compile_memory._prune_completed_manifests()

    assert (directory / f"{appended['generation_id']}.json").exists()
    assert len(list(directory.glob("*.json"))) <= 3


def test_completed_manifest_pruning_moves_owned_bytes_to_retired_store(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 1)
    active_dir = state_root / "run" / "compile-manifests"
    active_dir.mkdir(parents=True)
    generation_ids = ["4" * 64, "5" * 64]
    originals = {}
    for index, generation_id in enumerate(generation_ids):
        path = active_dir / f"{generation_id}.json"
        originals[generation_id] = f"owned manifest {index}\n".encode()
        path.write_bytes(originals[generation_id])
    state["compile_generation_completed"] = generation_ids

    compile_memory._prune_completed_manifests()

    retired_id = generation_ids[0]
    assert not (active_dir / f"{retired_id}.json").exists()
    [retired] = (state_root / "run" / "retired-manifests").glob(
        f"{retired_id}.*.json"
    )
    assert retired.read_bytes() == originals[retired_id]
    assert (active_dir / f"{generation_ids[1]}.json").read_bytes() == originals[
        generation_ids[1]
    ]


def test_exact_completed_request_replays_from_retired_journal_and_manifest(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 0)
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-19-retired-replay.md",
        [_block("12:00:00", "Completed IDs remain exactly replayable after retirement.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    first = compile_memory.apply_compile_batch(request, _response(), False)

    assert first["ok"] is True
    active_journal = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    active_manifest = (
        state_root
        / "run"
        / "compile-manifests"
        / f"{request['generation_id']}.json"
    )
    assert not active_journal.exists()
    assert not active_manifest.exists()
    [retired_journal] = (state_root / "run" / "retired-journals").glob(
        f"{request['batch_id']}.*.json"
    )
    [retired_manifest] = (state_root / "run" / "retired-manifests").glob(
        f"{request['generation_id']}.*.json"
    )
    retired_before = (retired_journal.read_bytes(), retired_manifest.read_bytes())

    replayed = compile_memory.apply_compile_batch(request, _response(), False)

    assert replayed == {"ok": True, "status": "already_applied"}
    assert (active_journal.read_bytes(), active_manifest.read_bytes()) == retired_before
    assert not list((state_root / "run" / "retired-journals").glob(
        f"{request['batch_id']}.*.json"
    ))
    assert not list((state_root / "run" / "retired-manifests").glob(
        f"{request['generation_id']}.*.json"
    ))


def test_invalidated_retired_generation_reactivates_without_duplicate_ids(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 0)
    quote = "Retired replay must move one exact generation back to active storage."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-19-retired-reactivation.md",
        [_block("12:00:00", quote)],
    )
    slug = "retired-reactivation"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    original = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        original,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )["ok"]

    for _restart in range(2):
        target.unlink()
        replay = compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )
        assert replay["batch_id"] == original["batch_id"]
        assert replay["generation_id"] == original["generation_id"]

        result = compile_memory.apply_compile_batch(replay, _response(), False)

        assert result["ok"] is True
        assert target.is_file()
        journal_records = [
            state_root
            / "run"
            / "compile-journal"
            / f"{original['batch_id']}.json",
            *(state_root / "run" / "retired-journals").glob(
                f"{original['batch_id']}.*.json"
            ),
        ]
        manifest_records = [
            state_root
            / "run"
            / "compile-manifests"
            / f"{original['generation_id']}.json",
            *(state_root / "run" / "retired-manifests").glob(
                f"{original['generation_id']}.*.json"
            ),
        ]
        assert len([path for path in journal_records if path.exists()]) == 1
        assert len([path for path in manifest_records if path.exists()]) == 1


def test_legacy_empty_manifest_reactivates_instead_of_coexisting(compile_env, monkeypatch):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 0)
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-19-retired-empty-manifest.md",
        [],
    )

    assert compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    ) == {"pending": False}
    generation_id = state["compiled_daily_receipts"][daily.name]["generation_id"]
    manifest = compile_memory._load_manifest(generation_id)
    assert manifest["batch_ids"] == []
    retired_dir = state_root / "run" / "retired-manifests"
    assert len(list(retired_dir.glob(f"{generation_id}.*.json"))) == 1

    compile_memory._write_new_manifest(json.loads(json.dumps(manifest)))

    assert (
        state_root / "run" / "compile-manifests" / f"{generation_id}.json"
    ).is_file()
    assert not list(retired_dir.glob(f"{generation_id}.*.json"))


@pytest.mark.parametrize("kind", ("journal", "manifest"))
def test_retired_reactivation_replacement_preserves_foreign_bytes(
    compile_env,
    monkeypatch,
    kind,
):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 0)
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-19-reactivation-race-{kind}.md",
        [_block("12:00:00", f"Retired {kind} reactivation is race safe.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(request, _response(), False)["ok"]
    suffix = "journals" if kind == "journal" else "manifests"
    item_id = request["batch_id"] if kind == "journal" else request["generation_id"]
    retired_dir = state_root / "run" / f"retired-{suffix}"
    [retired] = retired_dir.glob(f"{item_id}.*.json")
    original = retired.read_bytes()
    checked = state_root / f"checked-reactivation-{kind}.json"
    racer = state_root / f"foreign-reactivation-{kind}.tmp"
    foreign = f"FOREIGN {kind.upper()} REACTIVATION BYTES\n".encode()
    racer.write_bytes(foreign)
    real_rename = compile_memory._rename_retired_child
    raced = False

    def replace_before_reactivation(source_bound, source, target_bound, destination):
        nonlocal raced
        if not raced and source_bound.path == retired_dir:
            raced = True
            os.replace(source_bound.path / source, checked)
            os.replace(racer, source_bound.path / source)
        return real_rename(source_bound, source, target_bound, destination)

    monkeypatch.setattr(
        compile_memory,
        "_rename_retired_child",
        replace_before_reactivation,
    )

    result = compile_memory.apply_compile_batch(request, _response(), False)

    assert raced is True
    assert result["ok"] is False
    assert checked.read_bytes() == original
    active_dir = "compile-journal" if kind == "journal" else "compile-manifests"
    active = state_root / "run" / active_dir / f"{item_id}.json"
    surviving = [active, *retired_dir.glob(f"{item_id}.*.json")]
    assert any(path.exists() and path.read_bytes() == foreign for path in surviving)


@pytest.mark.parametrize("limit", ("count", "bytes"))
def test_retired_manifest_quota_refuses_without_deleting_active_or_history(
    compile_env,
    monkeypatch,
    limit,
):
    compile_memory, _root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 0)
    monkeypatch.setattr(compile_memory, "MAX_RETIRED_MANIFESTS", 10, raising=False)
    monkeypatch.setattr(
        compile_memory,
        "MAX_RETIRED_MANIFEST_BYTES",
        1024 * 1024,
        raising=False,
    )
    active_dir = state_root / "run" / "compile-manifests"
    active_dir.mkdir(parents=True)
    first_id = "6" * 64
    first_path = active_dir / f"{first_id}.json"
    first_bytes = b"first owned manifest\n"
    first_path.write_bytes(first_bytes)
    state["compile_generation_completed"] = [first_id]
    if limit == "count":
        monkeypatch.setattr(compile_memory, "MAX_RETIRED_MANIFESTS", 1, raising=False)
    compile_memory._prune_completed_manifests()
    [first_retired] = (state_root / "run" / "retired-manifests").glob(
        f"{first_id}.*.json"
    )

    second_id = "7" * 64
    second_path = active_dir / f"{second_id}.json"
    second_bytes = b"second owned manifest\n"
    second_path.write_bytes(second_bytes)
    state["compile_generation_completed"] = [second_id]
    if limit == "bytes":
        monkeypatch.setattr(
            compile_memory,
            "MAX_RETIRED_MANIFEST_BYTES",
            len(first_bytes) + len(second_bytes) - 1,
            raising=False,
        )

    with pytest.raises(OSError, match=rf"retired manifest {limit} limit"):
        compile_memory._prune_completed_manifests()

    assert second_path.read_bytes() == second_bytes
    assert first_retired.read_bytes() == first_bytes


@pytest.mark.parametrize("kind", ("journal", "manifest"))
def test_retired_move_replacement_after_validation_restores_foreign_bytes(
    compile_env,
    monkeypatch,
    kind,
):
    compile_memory, _root, state_root, state = compile_env
    if kind == "journal":
        monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
        item_id = "a" * 64
        active = state_root / "run" / "compile-journal" / f"{item_id}.json"
        active.parent.mkdir(parents=True)
        original = _completed_journal_bytes(compile_memory, item_id)
        active.write_bytes(original)
        prune = compile_memory._prune_completed_journals
    else:
        monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 1)
        item_id = "b" * 64
        keep_id = "c" * 64
        active_dir = state_root / "run" / "compile-manifests"
        active_dir.mkdir(parents=True)
        active = active_dir / f"{item_id}.json"
        original = b"ORIGINAL MANIFEST MUST SURVIVE\n"
        active.write_bytes(original)
        (active_dir / f"{keep_id}.json").write_bytes(b"kept manifest\n")
        state["compile_generation_completed"] = [item_id, keep_id]
        prune = compile_memory._prune_completed_manifests

    foreign = b"FOREIGN RETIRED-RACE BYTES MUST SURVIVE\n"
    racer = state_root / f"{kind}-retired-racer.tmp"
    checked = state_root / f"checked-{kind}-retired.json"
    racer.write_bytes(foreign)
    real_sync = compile_memory._sync_retired_move
    raced = False

    def replace_after_validation(source_bound, retired_bound, retired_name):
        nonlocal raced
        retired = retired_bound.path / retired_name
        if not raced:
            raced = True
            os.replace(retired, checked)
            os.replace(racer, retired)
        return real_sync(source_bound, retired_bound, retired_name)

    monkeypatch.setattr(
        compile_memory,
        "_sync_retired_move",
        replace_after_validation,
    )

    with pytest.raises(
        (OSError, ValueError, compile_memory.CompilePreparationError),
        match="changed during retirement|changed during retired-store move",
    ):
        prune()

    assert raced is True
    assert active.read_bytes() == foreign
    assert checked.read_bytes() == original


@pytest.mark.parametrize("kind", ("journals", "manifests"))
def test_linked_retired_directory_blocks_pruning_without_external_mutation(
    compile_env,
    monkeypatch,
    kind,
):
    compile_memory, _root, state_root, state = compile_env
    external = state_root / f"external-retired-{kind}"
    external.mkdir(parents=True)
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"EXTERNAL RETIRED BYTES MUST SURVIVE\n")
    retired_dir = state_root / "run" / f"retired-{kind}"
    _directory_link_or_skip(retired_dir, external)

    if kind == "journals":
        monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
        item_id = "6" * 64
        active = state_root / "run" / "compile-journal" / f"{item_id}.json"
        active.parent.mkdir(parents=True)
        active.write_bytes(_completed_journal_bytes(compile_memory, item_id))
        prune = compile_memory._prune_completed_journals
    else:
        monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 1)
        item_id = "7" * 64
        keep_id = "8" * 64
        active_dir = state_root / "run" / "compile-manifests"
        active_dir.mkdir(parents=True)
        active = active_dir / f"{item_id}.json"
        active.write_bytes(b"owned manifest\n")
        (active_dir / f"{keep_id}.json").write_bytes(b"kept manifest\n")
        state["compile_generation_completed"] = [item_id, keep_id]
        prune = compile_memory._prune_completed_manifests

    before = sentinel.read_bytes()
    with pytest.raises((OSError, ValueError, compile_memory.CompilePreparationError)):
        prune()

    assert active.is_file()
    assert sentinel.read_bytes() == before


@pytest.mark.parametrize("kind", ("journals", "manifests"))
def test_full_retired_store_blocks_new_generation_before_manifest_write(
    compile_env,
    monkeypatch,
    kind,
):
    compile_memory, root, state_root, state = compile_env
    retired_dir = state_root / "run" / f"retired-{kind}"
    retired_dir.mkdir(parents=True)
    retired_id = "9" * 64
    (retired_dir / f"{retired_id}.{'a' * 32}.json").write_bytes(
        b"retired capacity owner\n"
    )
    if kind == "journals":
        monkeypatch.setattr(compile_memory, "MAX_RETIRED_JOURNALS", 1, raising=False)
        monkeypatch.setattr(
            compile_memory,
            "MAX_RETIRED_JOURNAL_BYTES",
            1024,
            raising=False,
        )
    else:
        monkeypatch.setattr(compile_memory, "MAX_RETIRED_MANIFESTS", 1, raising=False)
        monkeypatch.setattr(
            compile_memory,
            "MAX_RETIRED_MANIFEST_BYTES",
            1024,
            raising=False,
        )
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-20-retired-{kind}.md",
        [_block("12:00:00", f"Retired {kind} capacity is fail closed.")],
    )

    with pytest.raises(
        compile_memory.CompilePreparationError,
        match=rf"retired {kind[:-1]} store",
    ):
        compile_memory._create_generation_manifest(daily, 30_000, state)

    assert daily.name not in state.get("compile_generation_active", {})
    assert not list((state_root / "run" / "compile-manifests").glob("*.json"))


def test_cold_archive_audit_selects_only_disconnected_components(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, state = compile_env
    archived, archived_manifest, archived_journal = _retired_archive_component(
        compile_env, monkeypatch, "13-archive"
    )
    live, live_manifest, live_journal = _retired_archive_component(
        compile_env, monkeypatch, "14-live"
    )
    state["compile_sdk_progress"] = {
        "live.md": {
            "generation_id": live["generation_id"],
            "expected_batch_ids": [live["batch_id"]],
        }
    }
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)

    report = compile_memory.archive_retired_evidence("audit")

    assert report["status"] == "eligible"
    assert report["component_count"] == 1
    assert {(item["kind"], item["id"]) for item in report["artifacts"]} == {
        ("manifest", archived["generation_id"]),
        ("journal", archived["batch_id"]),
    }
    assert archived_manifest.is_file() and archived_journal.is_file()
    assert live_manifest.is_file() and live_journal.is_file()


def test_cold_archive_accepts_known_historical_prompt_contract(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, state = compile_env
    request, manifest_path, journal_path = _retired_archive_component(
        compile_env, monkeypatch, "21-historical-prompt"
    )
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    added_rules = (
        "5. related must be a JSON array of strings; use [] when empty.\n"
        "6. For create, count body_markdown words before returning each create. Title,\n"
        "   summary, evidence, and related do not count. Expand, trim, or omit the\n"
        "   operation unless body_markdown itself contains 150-400 words.\n"
    )
    for batch in manifest["batches"]:
        assert added_rules in batch["prompt"]
        batch["prompt"] = batch["prompt"].replace(added_rules, "", 1)
    manifest["manifest_sha256"] = compile_memory._manifest_digest(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = compile_memory.archive_retired_evidence("audit")

    assert report["status"] == "eligible"
    assert {(item["kind"], item["id"]) for item in report["artifacts"]} == {
        ("manifest", request["generation_id"]),
        ("journal", request["batch_id"]),
    }
    assert manifest_path.is_file() and journal_path.is_file()


def test_cold_archive_rejects_unknown_self_hashed_prompt_drift(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, state = compile_env
    _request, manifest_path, _journal_path = _retired_archive_component(
        compile_env, monkeypatch, "22-unknown-prompt"
    )
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["batches"][0]["prompt"] += "\nUNRECOGNIZED HISTORICAL RULE\n"
    manifest["manifest_sha256"] = compile_memory._manifest_digest(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest derivation check failed"):
        compile_memory.archive_retired_evidence("audit")


def test_cold_archive_backup_is_byte_exact_and_apply_is_approval_gated(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, state = compile_env
    _request, hot_manifest, hot_journal = _retired_archive_component(
        compile_env, monkeypatch, "15-backup"
    )
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    before = {
        "manifest": hot_manifest.read_bytes(),
        "journal": hot_journal.read_bytes(),
    }

    prepared = compile_memory.archive_retired_evidence("backup-only")
    manifest_path = Path(prepared["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert prepared["status"] == "prepared"
    assert manifest["approved"] is False
    assert hot_manifest.read_bytes() == before["manifest"]
    assert hot_journal.read_bytes() == before["journal"]
    for item in manifest["artifacts"]:
        archived = manifest_path.parent / item["archive_path"]
        assert archived.read_bytes() == before[item["kind"]]
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == item["sha256"]
    with pytest.raises(ValueError, match="not approved"):
        compile_memory.archive_retired_evidence("apply", manifest_path)
    assert hot_manifest.read_bytes() == before["manifest"]
    assert hot_journal.read_bytes() == before["journal"]


def test_cold_archive_apply_resumes_after_staging_crash_and_verifies(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, state = compile_env
    _request, hot_manifest, hot_journal = _retired_archive_component(
        compile_env, monkeypatch, "16-resume"
    )
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    prepared = compile_memory.archive_retired_evidence("backup-only")
    manifest_path = Path(prepared["manifest"])
    _approve_archive_manifest(manifest_path)
    real_remove = compile_memory._cold_archive_remove_staged
    crashed = False

    def crash_after_first_stage(transaction_bound, relative_path, expected):
        nonlocal crashed
        if not crashed:
            crashed = True
            real_remove(transaction_bound, relative_path, expected)
            raise RuntimeError("synthetic cold archive crash")
        return real_remove(transaction_bound, relative_path, expected)

    monkeypatch.setattr(
        compile_memory,
        "_cold_archive_remove_staged",
        crash_after_first_stage,
    )
    with pytest.raises(RuntimeError, match="synthetic cold archive crash"):
        compile_memory.archive_retired_evidence("apply", manifest_path)
    monkeypatch.setattr(
        compile_memory,
        "_cold_archive_remove_staged",
        real_remove,
    )

    applied = compile_memory.archive_retired_evidence("apply", manifest_path)
    verified = compile_memory.archive_retired_evidence("verify", manifest_path)

    assert crashed is True
    assert applied["status"] == "applied"
    assert verified["status"] == "verified"
    assert not hot_manifest.exists()
    assert not hot_journal.exists()
    transaction = json.loads(
        manifest_path.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["status"] == "committed"
    assert all(item["status"] == "removed" for item in transaction["artifacts"])


def test_cold_archive_apply_preserves_aba_replacement(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, state = compile_env
    _request, hot_manifest, _hot_journal = _retired_archive_component(
        compile_env, monkeypatch, "17-aba"
    )
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    prepared = compile_memory.archive_retired_evidence("backup-only")
    manifest_path = Path(prepared["manifest"])
    _approve_archive_manifest(manifest_path)
    checked = state_root / "checked-cold-archive-original.json"
    replacement = b"FOREIGN RETIRED MANIFEST MUST SURVIVE\n"
    real_move = compile_memory._cold_archive_stage_hot_artifact
    raced = False

    def replace_before_stage(
        source_bound,
        source_name,
        transaction_bound,
        staged_name,
        expected,
    ):
        nonlocal raced
        if not raced and source_name == hot_manifest.name:
            raced = True
            os.replace(hot_manifest, checked)
            hot_manifest.write_bytes(replacement)
        return real_move(
            source_bound,
            source_name,
            transaction_bound,
            staged_name,
            expected,
        )

    monkeypatch.setattr(
        compile_memory,
        "_cold_archive_stage_hot_artifact",
        replace_before_stage,
    )

    with pytest.raises(ValueError, match="changed before archive removal"):
        compile_memory.archive_retired_evidence("apply", manifest_path)

    assert raced is True
    assert hot_manifest.read_bytes() == replacement
    assert checked.is_file()


def test_cold_archive_rejects_linked_retired_store_without_external_mutation(
    compile_env,
):
    compile_memory, _root, state_root, _state = compile_env
    external = state_root / "external-retired-manifests"
    external.mkdir()
    sentinel = external / f"{'a' * 64}.{'b' * 32}.json"
    sentinel.write_bytes(b"EXTERNAL RETIRED MANIFEST\n")
    retired = state_root / "run" / "retired-manifests"
    _directory_link_or_skip(retired, external)

    with pytest.raises((OSError, ValueError)):
        compile_memory.archive_retired_evidence("audit")

    assert sentinel.read_bytes() == b"EXTERNAL RETIRED MANIFEST\n"


def test_cold_archive_rejects_hardlinked_artifact(compile_env):
    compile_memory, _root, state_root, _state = compile_env
    retired = state_root / "run" / "retired-manifests"
    retired.mkdir()
    source = retired / f"{'c' * 64}.{'d' * 32}.json"
    source.write_bytes(b"{}")
    alias = state_root / "retired-manifest-hardlink.json"
    _hard_link_or_skip(source, alias)

    with pytest.raises((OSError, ValueError), match="unsafe"):
        compile_memory.archive_retired_evidence("audit")

    assert source.read_bytes() == b"{}"
    assert alias.read_bytes() == b"{}"


def test_cold_archive_rejects_malformed_queue_task(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, state = compile_env
    _retired_archive_component(compile_env, monkeypatch, "18-queue-schema")
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    queue = state_root / "run" / "queue"
    queue.mkdir()
    task_id = "20261118-120000-1234abcd"
    (queue / f"{task_id}.json").write_text(
        json.dumps({"id": task_id, "type": "compile"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="queue integrity"):
        compile_memory.archive_retired_evidence("audit")


def test_cold_archive_apply_rejects_component_that_became_live(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, state = compile_env
    request, hot_manifest, hot_journal = _retired_archive_component(
        compile_env, monkeypatch, "19-live-drift"
    )
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    prepared = compile_memory.archive_retired_evidence("backup-only")
    manifest_path = Path(prepared["manifest"])
    _approve_archive_manifest(manifest_path)
    state["compile_sdk_progress"] = {
        "live.md": {
            "generation_id": request["generation_id"],
            "expected_batch_ids": [request["batch_id"]],
        }
    }

    with pytest.raises(ValueError, match="became live"):
        compile_memory.archive_retired_evidence("apply", manifest_path)

    assert hot_manifest.is_file()
    assert hot_journal.is_file()


def test_cold_archive_verify_rejects_payload_tamper(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, _state_root, state = compile_env
    request, _hot_manifest, _hot_journal = _retired_archive_component(
        compile_env, monkeypatch, "20-verify-tamper"
    )
    _bind_archive_state_snapshot(compile_memory, monkeypatch, state)
    prepared = compile_memory.archive_retired_evidence("backup-only")
    manifest_path = Path(prepared["manifest"])
    _approve_archive_manifest(manifest_path)
    assert compile_memory.archive_retired_evidence(
        "apply", manifest_path
    )["status"] == "applied"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archived_manifest = next(
        item for item in manifest["artifacts"] if item["id"] == request["generation_id"]
    )
    (manifest_path.parent / archived_manifest["archive_path"]).write_bytes(
        b"TAMPERED COLD ARCHIVE PAYLOAD\n"
    )

    with pytest.raises(ValueError, match="payload verification"):
        compile_memory.archive_retired_evidence("verify", manifest_path)


def test_cold_archive_cli_dispatches_and_validates_manifest_arguments(
    monkeypatch,
    capsys,
    tmp_path,
):
    import compile_memory

    calls = []

    def archive(phase, manifest=None):
        calls.append((phase, manifest))
        return {"status": "verified"}

    monkeypatch.setattr(compile_memory, "archive_retired_evidence", archive)
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compile_memory.py",
            "--archive-retired",
            "verify",
            "--manifest",
            str(manifest),
        ],
    )

    assert compile_memory.main() == 0
    assert calls == [("verify", manifest)]
    assert json.loads(capsys.readouterr().out)["status"] == "verified"

    monkeypatch.setattr(
        sys,
        "argv",
        ["compile_memory.py", "--archive-retired", "apply"],
    )
    assert compile_memory.main() == 2
    assert "invalid cold archive arguments" in capsys.readouterr().err


def test_atomic_write_flushes_file_before_replace_and_syncs_parent(
    tmp_path, monkeypatch
):
    import memory_state

    events = []
    real_replace = (
        memory_state._move_file_write_through_windows
        if os.name == "nt"
        else memory_state.os.replace
    )
    monkeypatch.setattr(
        memory_state.os,
        "fsync",
        lambda fd: events.append(("fsync", fd)),
    )
    monkeypatch.setattr(
        memory_state,
        "_sync_parent_directory",
        lambda path: events.append(("parent", Path(path))),
        raising=False,
    )

    def replace(source, target, **kwargs):
        events.append(("replace", Path(target)))
        real_replace(source, target, **kwargs)

    if os.name == "nt":
        monkeypatch.setattr(memory_state, "_move_file_write_through_windows", replace)
    else:
        monkeypatch.setattr(memory_state.os, "replace", replace)
    target = tmp_path / "durable.json"

    memory_state.atomic_write(target, "durable")

    names = [event[0] for event in events]
    assert names.index("fsync") < names.index("replace") < names.index("parent")
    assert target.read_text(encoding="utf-8") == "durable"


def test_atomic_write_concurrent_writers_use_unique_temp_files(tmp_path, monkeypatch):
    import memory_state

    target = tmp_path / "durable.json"
    contenders = threading.Barrier(2)
    real_replace = (
        memory_state._move_file_write_through_windows
        if os.name == "nt"
        else memory_state.os.replace
    )
    sources = []
    source_lock = threading.Lock()
    replace_calls = 0

    def synchronized_replace(source, destination, **kwargs):
        nonlocal replace_calls
        with source_lock:
            sources.append(Path(source))
            replace_calls += 1
            synchronize = replace_calls <= 2
        if synchronize:
            contenders.wait(timeout=5)
        real_replace(source, destination, **kwargs)

    if os.name == "nt":
        monkeypatch.setattr(
            memory_state,
            "_move_file_write_through_windows",
            synchronized_replace,
        )
    else:
        monkeypatch.setattr(memory_state.os, "replace", synchronized_replace)
    values = ('{"writer":1}\n', '{"writer":2}\n')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(memory_state.atomic_write, target, value) for value in values]
        for future in futures:
            future.result(timeout=5)

    assert len(set(sources)) == 2
    assert all(source.parent == target.parent for source in sources)
    assert target.read_text(encoding="utf-8") in values
    assert not [source for source in sources if source.exists()]


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_atomic_write_preserves_existing_target_mode(tmp_path):
    import stat

    import memory_state

    target = tmp_path / "mode-preserved.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)

    memory_state.atomic_write(target, "new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_compile_lock_file_remains_and_unlocks_when_context_exits(compile_env):
    compile_memory, _root, state_root, _state = compile_env
    lock_file = state_root / "run" / "compile.pid"

    with compile_memory._global_compile_lock(timeout=0.1):
        assert lock_file.exists()

    assert lock_file.exists()
    with compile_memory._global_compile_lock(timeout=0.1):
        pass


def test_compile_lock_is_released_by_os_when_holder_process_terminates(
    compile_env,
):
    compile_memory, _root, state_root, _state = compile_env
    lock_file = state_root / "run" / "compile.pid"
    lock_file.write_text("fixed lock file\n", encoding="utf-8")
    script = (
        "import sys, time; from pathlib import Path; import compile_memory; "
        "compile_memory.STATE_ROOT = Path(sys.argv[1]); "
        "lock = compile_memory._global_compile_lock(timeout=1); "
        "lock.__enter__(); print('locked', flush=True); time.sleep(60)"
    )
    env = os.environ.copy()
    scripts = str(Path(compile_memory.__file__).resolve().parent)
    env["PYTHONPATH"] = scripts + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(state_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        process.terminate()
        process.wait(timeout=5)

        with compile_memory._global_compile_lock(timeout=1):
            pass
        assert lock_file.read_text(encoding="utf-8") == "fixed lock file\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_sdk_lock_timeout_is_durable_failure(compile_env, monkeypatch):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-08-03.md",
        [_block("12:00:00", "lock timeout evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "COMPILE_LOCK_TIMEOUT_SECONDS", 0.02)
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with compile_memory._global_compile_lock(timeout=1):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=2)
    try:
        result = compile_memory.apply_compile_batch(
            request, '{"operations": [], "audit": {}}', False
        )
    finally:
        release.set()
        holder.join(timeout=5)

    assert result["ok"] is False
    assert result["status"] == "lock_timeout"
    assert state["last_compile_sdk_error"]["stage"] == "lock"
    assert "Could not acquire compile lock" in state["last_compile_sdk_error"]["error"]
    assert "stage=lock" in (
        state_root / "logs" / "compile-sdk-last.log"
    ).read_text(encoding="utf-8")


def test_direct_lock_timeout_returns_nonzero_without_running_compile(
    compile_env, monkeypatch, capsys
):
    compile_memory, _root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "COMPILE_LOCK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(sys, "argv", ["compile_memory.py"])
    monkeypatch.setattr(
        compile_memory,
        "_run",
        lambda _args: pytest.fail("compile must not run without the lock"),
    )
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with compile_memory._global_compile_lock(timeout=1):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=2)
    try:
        result = compile_memory.main()
    finally:
        release.set()
        holder.join(timeout=5)

    captured = capsys.readouterr()
    assert result != 0
    assert "Traceback" not in captured.err
    assert "compile lock" in captured.err
    assert state["last_compile_sdk_error"]["stage"] == "lock"
    assert "stage=lock" in (
        state_root / "logs" / "compile-sdk-last.log"
    ).read_text(encoding="utf-8")


def test_execute_plan_uses_explicit_knowledge_directory_without_global_swap(
    compile_env, monkeypatch
):
    compile_memory, root, _state_root, _state = compile_env
    original = compile_memory.KNOWLEDGE
    alternate = root / "alternate-notes"
    alternate.mkdir()
    monkeypatch.setattr(compile_memory, "_verify_evidence", lambda *_args: (1, 0))
    plan = {
        "operations": [
            {
                "action": "create",
                "category": "patterns",
                "slug": "explicit-root",
                "title": "Explicit Root",
                "summary": "No global swap.",
                "body_section": "Lesson",
                "body_markdown": "body",
                "evidence": [{"daily_date": "x", "timestamp": "x", "claim": "x"}],
                "related": [],
                "project_slug": _SYNTHETIC_PROJECT_SLUG,
                "project_root": _SYNTHETIC_PROJECT_ROOT,
            }
        ],
        "audit": {},
    }

    compile_memory._execute_plan(
        plan, [], False, knowledge_dir=alternate, source_request=None
    )

    assert compile_memory.KNOWLEDGE == original
    assert (alternate / "explicit-root.md").exists()


@pytest.mark.parametrize("wrapper", ("plain", "extracted"))
@pytest.mark.parametrize(
    "duplicate_raw",
    (
        '{"operations":[],"operations":[],"audit":{}}',
        '{"operations":[],"audit":{"verified":0,"verified":1}}',
        '{"operations":[{"action":"create","action":"update"}],"audit":{}}',
        '{"operations":[{"evidence":[{"claim":"one","claim":"two"}]}],"audit":{}}',
    ),
    ids=("root", "audit", "operation", "nested-evidence"),
)
def test_provider_duplicate_json_keys_are_durably_rejected_and_retryable(
    compile_env,
    wrapper,
    duplicate_raw,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-10-duplicate-json.md",
        [_block("12:00:00", "duplicate key evidence")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    raw = (
        duplicate_raw
        if wrapper == "plain"
        else f"provider preamble\n```json\n{duplicate_raw}\n```\n"
    )

    result = compile_memory.apply_compile_batch(request, raw, False)

    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    if wrapper == "plain":
        assert "duplicate JSON key" in result["error"]
    else:
        assert "JSON" in result["error"]
    assert not list((root / "knowledge" / "notes").glob("*.md"))
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert not (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    ).exists()
    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert retry["batch_id"] == request["batch_id"]


def test_atomic_text_write_preserves_crlf_bytes_without_translation(tmp_path):
    import memory_state

    target = tmp_path / "newline-safe.md"
    content = "alpha\r\nbeta\r\n"

    memory_state.atomic_write(target, content)

    assert target.read_bytes() == content.encode("utf-8")


def test_journaled_crlf_update_preserves_prefix_and_uses_one_convention(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "CRLF updates preserve every admitted source byte."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-11-crlf.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "crlf-update.md"
    original = (
        "---\r\n"
        "type: pattern\r\n"
        "status: active\r\n"
        f"project: {_SYNTHETIC_PROJECT_SLUG}\r\n"
        f"project_root: {json.dumps(_SYNTHETIC_PROJECT_ROOT)}\r\n"
        "---\r\n\r\n"
        "# CRLF Update\r\n\r\n"
        "One-sentence summary: Existing bytes.  \r\n"
    ).encode()
    target.write_bytes(original)
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        target.stem,
        quote,
        action="update",
        body="CRLF_BODY\n" + _word_body(150),
    )

    first = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )
    after_first = target.read_bytes()
    retry = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )

    assert first["ok"] is True
    assert retry["ok"] is True
    assert after_first.startswith(original)
    assert b"\r\r\n" not in after_first
    assert b"\n" not in after_first.replace(b"\r\n", b"")
    assert target.read_bytes() == after_first
    assert after_first.count(b"CRLF_BODY") == 1


def test_oversized_update_is_rejected_before_journal_or_mutation(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Rendered updates are admitted against the final byte budget."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-12-result-bound.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "result-bound.md"
    prefix = (
        "---\n"
        "type: pattern\n"
        "status: active\n"
        f"project: {_SYNTHETIC_PROJECT_SLUG}\n"
        f"project_root: {json.dumps(_SYNTHETIC_PROJECT_ROOT)}\n"
        "---\n\n"
        "# Result Bound\n\n"
        "One-sentence summary: Existing bounded page.\n\n"
    ).encode()
    original = prefix + b"x" * (59 * 1024 - len(prefix))
    target.write_bytes(original)
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        target.stem,
        quote,
        action="update",
        body=("oversized " * 1200).strip(),
    )

    result = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "page byte limit" in result["error"]
    assert target.read_bytes() == original


def test_rendered_update_at_exact_byte_limit_is_accepted(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "The exact rendered byte boundary remains admissible."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-12-exact-result-bound.md",
        [_block("12:00:00", quote)],
    )
    target = root / "knowledge" / "notes" / "exact-result-bound.md"
    _write_note(target, "Exact Result Bound", "Existing exact-boundary page.")
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        target.stem,
        quote,
        action="update",
        body="exact-boundary " + _word_body(150),
    )
    existing = compile_memory._read_knowledge_page_snapshot(target)[0]
    rendered = compile_memory._render_operation_result(
        operation,
        existing,
        compile_memory._operation_marker(None, 0, operation),
        compile_memory._operation_replay_fingerprint(operation),
    )
    exact_limit = len(rendered.encode("utf-8"))
    monkeypatch.setattr(compile_memory, "MAX_KNOWLEDGE_PAGE_BYTES", exact_limit)

    result = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )

    assert result["ok"] is True
    assert len(target.read_bytes()) == exact_limit


def test_applied_journal_persists_exact_operation_effect_snapshot(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Applied operations retain exact snapshots for final validation."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-effect-snapshot.md",
        [_block("12:00:00", quote)],
    )
    slug = "effect-snapshot"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )

    assert result["status"] == "index_pending"
    journal = json.loads(
        (
            state_root
            / "run"
            / "compile-journal"
            / f"{request['batch_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["operation_effects"] == [
        {
            "version": 2,
            "target": target.name,
            "before": None,
            "after": compile_memory._read_knowledge_page_snapshot(target)[1],
            "retained_artifact": None,
        }
    ]


def test_finalization_revalidates_exact_effects_from_every_manifest_batch(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    first_quote = "The first batch effect must survive until final publication."
    first_evidence = f"Rule: {first_quote} " + "a" * 14_000
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-all-batch-effects.md",
        [
            _block("12:00:00", first_quote + " " + "a" * 14_000),
            _block("12:01:00", "A second bounded batch. " + "b" * 14_000),
        ],
    )
    slug = "all-batch-effects"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    first_result = compile_memory.apply_compile_batch(
        first,
        _response(_admission_operation(daily, slug, first_evidence)),
        False,
    )
    assert first_result["daily_complete"] is False
    target.write_text(
        target.read_text(encoding="utf-8") + "\nFOREIGN UNJOURNALED APPEND\n",
        encoding="utf-8",
    )
    final = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )

    result = compile_memory.apply_compile_batch(final, _response(), False)

    assert result["ok"] is False
    assert result["status"] == "index_pending"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert state["compile_index_pending"]["batch_id"] == final["batch_id"]


def test_manifest_effect_chain_accepts_later_journaled_update_to_same_target(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    first_quote = "The first batch creates the chained target."
    second_quote = "The second batch appends through a journaled update."
    first_evidence = f"Rule: {first_quote} " + "a" * 14_000
    second_evidence = f"Rule: {second_quote} " + "b" * 14_000
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-effect-chain.md",
        [
            _block("12:00:00", first_quote + " " + "a" * 14_000),
            _block("12:01:00", second_quote + " " + "b" * 14_000),
        ],
    )
    slug = "effect-chain"
    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    assert compile_memory.apply_compile_batch(
        first,
        _response(_admission_operation(daily, slug, first_evidence)),
        False,
    )["ok"]
    second = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    operation = _admission_operation(
        daily,
        slug,
        second_evidence,
        action="update",
        timestamp="12:01:00",
    )

    result = compile_memory.apply_compile_batch(
        second,
        _response(operation),
        False,
    )

    assert result["ok"] is True
    assert result["daily_complete"] is True
    journals = []
    for request in (first, second):
        journals.append(
            json.loads(
                (
                    state_root
                    / "run"
                    / "compile-journal"
                    / f"{request['batch_id']}.json"
                ).read_text(encoding="utf-8")
            )
        )
    assert journals[0]["operation_effects"][0]["after"] == (
        journals[1]["operation_effects"][0]["before"]
    )


def test_final_index_requires_target_as_the_primary_entry_link(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Index membership comes only from an entry's primary link."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-exact-index-link.md",
        [_block("12:00:00", quote)],
    )
    slug = "exact-index-link"
    index = root / "knowledge" / "index.md"
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    def rebuild_with_decoy_summary():
        index.write_text(
            "# Index\n\n"
            "- [[knowledge/notes/different-page]] — "
            f"summary mentions [[knowledge/notes/{slug}]]\n",
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(compile_memory, "INDEX", index)
    monkeypatch.setattr(compile_memory, "rebuild_index", rebuild_with_decoy_summary)

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )

    assert result["ok"] is False
    assert result["status"] == "index_pending"
    assert daily.name not in state.get("compiled_daily_hashes", {})


@pytest.mark.parametrize("mutation", ("delete", "replace"))
def test_final_effect_revalidation_blocks_hash_after_rebuild_race(
    compile_env,
    monkeypatch,
    mutation,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Finalization revalidates every accepted journal effect."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-13-final-{mutation}.md",
        [_block("12:00:00", quote)],
    )
    slug = f"final-effect-{mutation}"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    index = root / "knowledge" / "index.md"
    monkeypatch.setattr(compile_memory, "INDEX", index)
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(daily, slug, quote)

    def rebuild_then_race():
        index.write_text(
            f"# Index\n\n- [[knowledge/notes/{slug}]]\n",
            encoding="utf-8",
        )
        assert target.is_file()
        if mutation == "delete":
            target.unlink()
        else:
            replacement = target.with_name("foreign-final-effect.md")
            replacement.write_bytes(b"FOREIGN FINAL EFFECT\n")
            os.replace(replacement, target)
        return True

    monkeypatch.setattr(compile_memory, "rebuild_index", rebuild_then_race)

    result = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )

    assert result["ok"] is False
    assert result["status"] == "index_pending"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    journal = json.loads(
        (
            state_root
            / "run"
            / "compile-journal"
            / f"{request['batch_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["status"] == "index_pending"
    assert state["compile_index_pending"]["batch_id"] == request["batch_id"]


@pytest.mark.parametrize("race_point", ("inside-state-update", "after-state-write"))
def test_effect_change_at_state_publication_unpublishes_compiled_hash(
    compile_env,
    monkeypatch,
    race_point,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Trusted hashes require live operation effects at publication."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-13-{race_point}.md",
        [_block("12:00:00", quote)],
    )
    slug = f"publication-race-{race_point}"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    real_update_state = compile_memory.update_state
    raced = False

    def update_state_with_race(mutator):
        nonlocal raced
        is_publication = getattr(mutator, "__name__", "") == "_complete_index"
        if is_publication and race_point == "inside-state-update" and not raced:
            raced = True
            target.unlink()
        updated = real_update_state(mutator)
        if is_publication and race_point == "after-state-write" and not raced:
            raced = True
            target.unlink()
        return updated

    monkeypatch.setattr(compile_memory, "update_state", update_state_with_race)

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )

    assert raced is True
    assert result["ok"] is False
    assert result["status"] == "index_pending"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert daily.name not in state.get("compiled_daily_receipts", {})
    assert state["compile_index_pending"]["batch_id"] == request["batch_id"]
    assert state["last_compile_status"] == "error"
    resumed = compile_memory._resume_pending_index_if_any()
    assert resumed is not None
    assert resumed["ok"] is True
    replayed_receipt = state["compiled_daily_receipts"][daily.name]
    assert replayed_receipt["effects"]
    assert replayed_receipt["targets"]
    assert daily.name in state["compile_daily_checkpoints"]
    assert compile_memory.trusted_compiled_daily_hashes(state, root=root) == {
        daily.name: request["dailies"][0]["sha256"]
    }
    journal = json.loads(
        (
            state_root
            / "run"
            / "compile-journal"
            / f"{request['batch_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["status"] == "complete"


def test_final_journal_is_durable_before_effect_receipt_publication(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-journal-before-receipt.md",
        [_block("12:00:00", "The final journal status precedes receipt publication.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    real_write_journal = compile_memory._write_journal
    receipt_seen_at_completion = []
    failed_once = False

    def fail_first_completion(journal):
        nonlocal failed_once
        if journal.get("status") == "complete" and not failed_once:
            failed_once = True
            receipt_seen_at_completion.append(
                daily.name in state.get("compiled_daily_receipts", {})
            )
            raise OSError("injected final journal durability failure")
        return real_write_journal(journal)

    monkeypatch.setattr(compile_memory, "_write_journal", fail_first_completion)

    first = compile_memory.apply_compile_batch(request, _response(), False)

    assert receipt_seen_at_completion == [False]
    assert first["ok"] is False
    assert first["status"] == "index_pending"
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert daily.name not in state.get("compiled_daily_receipts", {})
    assert state["compile_index_pending"]["batch_id"] == request["batch_id"]
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == (
        "index_pending"
    )

    resumed = compile_memory._resume_pending_index_if_any()

    assert resumed is not None
    assert resumed["ok"] is True
    assert daily.name in state["compiled_daily_receipts"]
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "complete"


def test_successful_hash_persists_bounded_self_contained_effect_receipt(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "A trusted compiled hash carries its durable effect receipt."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-effect-receipt.md",
        [_block("12:00:00", quote)],
    )
    slug = "effect-receipt"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )

    assert result["ok"] is True
    receipt = state["compiled_daily_receipts"][daily.name]
    assert receipt["version"] == 2
    assert receipt["daily_sha256"] == request["dailies"][0]["sha256"]
    assert receipt["generation_id"] == request["generation_id"]
    assert receipt["journal_ids"] == [request["batch_id"]]
    assert receipt["index"] == {
        "generation_id": request["generation_id"],
        "entries": [f"knowledge/notes/{slug}"],
    }
    assert receipt["targets"] == [
        {
            "target": target.name,
            "current": compile_memory._read_knowledge_page_snapshot(target)[1],
        }
    ]
    [effect] = receipt["effects"]
    assert effect["journal_id"] == request["batch_id"]
    assert effect["operation_index"] == 0
    assert effect["target"] == target.name
    assert effect["after"] == receipt["targets"][0]["current"]
    assert effect["marker"] in target.read_text(encoding="utf-8")
    assert effect["fingerprint"] in target.read_text(encoding="utf-8")
    assert effect["project_slug"] == _SYNTHETIC_PROJECT_SLUG
    assert effect["project_root"] == _SYNTHETIC_PROJECT_ROOT
    assert len(json.dumps(receipt).encode("utf-8")) <= (
        compile_memory.MAX_COMPILE_RECEIPT_BYTES
    )


def test_compiled_hash_without_effect_receipt_is_pending(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-missing-receipt.md",
        [_block("12:00:00", "A raw hash alone is not completion evidence.")],
    )
    state["compiled_daily_hashes"] = {
        daily.name: compile_memory._daily_snapshot_hash(daily)
    }
    args = argparse.Namespace(file=None, all=False, sdk_paths=None)

    assert compile_memory.select_dailies(args, state) == [daily]


def test_prepare_does_not_skip_matching_hash_without_effect_receipt(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-13-prepare-missing-receipt.md",
        [_block("12:00:00", "Preparation must distrust an unreceipted hash.")],
    )
    state["compiled_daily_hashes"] = {
        daily.name: compile_memory._daily_snapshot_hash(daily)
    }

    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    assert request["pending"] is True
    assert request["dailies"][0]["path"].endswith(daily.name)


@pytest.mark.parametrize(
    ("mutation", "pending"),
    (
        ("append", False),
        ("delete", True),
        ("replace", True),
        ("strip-body", True),
    ),
)
def test_effect_receipt_accepts_only_same_identity_additive_evolution(
    compile_env,
    mutation,
    pending,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Receipt validation distinguishes additive history from stale effects."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-14-receipt-{mutation}.md",
        [_block("12:00:00", quote)],
    )
    slug = f"receipt-{mutation}"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )["ok"]
    original = target.read_bytes()
    receipt = state["compiled_daily_receipts"][daily.name]
    marker = receipt["effects"][0]["marker"].encode()
    fingerprint = receipt["effects"][0]["fingerprint"].encode()

    if mutation == "append":
        with target.open("ab") as handle:
            handle.write(b"\nAUTHORIZED ADDITIVE HISTORY\n")
    elif mutation == "delete":
        target.unlink()
    elif mutation == "replace":
        replacement = target.with_name(f"{slug}-replacement.md")
        replacement.write_bytes(original + b"\nREPLACED IDENTITY\n")
        os.replace(replacement, target)
    else:
        target.write_bytes(marker + b"\n" + fingerprint + b"\n")

    args = argparse.Namespace(file=None, all=False, sdk_paths=None)
    selected = compile_memory.select_dailies(args, state)

    assert (daily in selected) is pending


@pytest.mark.skipif(
    os.name != "nt",
    reason="pre-3.12 st_dev width compatibility is Windows-specific",
)
def test_pre_312_windows_device_identity_remains_replayable(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    quote = "Persisted effects survive the Windows st_dev width change."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-14-windows-device-width.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "windows-device-width", quote)),
        False,
    )["ok"]
    receipt = json.loads(json.dumps(state["compiled_daily_receipts"][daily.name]))
    current_device = receipt["targets"][0]["current"]["identity"][0]
    alternate_width = (
        current_device & 0xFFFFFFFF
        if current_device > 0xFFFFFFFF
        else current_device | (1 << 63)
    )
    receipt["targets"][0]["current"]["identity"][0] = alternate_width

    assert memory_state.is_compile_receipt_valid(
        receipt,
        daily.name,
        request["dailies"][0]["sha256"],
        root=root,
    )
    wrong_device = json.loads(json.dumps(receipt))
    wrong_device["targets"][0]["current"]["identity"][0] ^= 1
    assert not memory_state.is_compile_receipt_valid(
        wrong_device,
        daily.name,
        request["dailies"][0]["sha256"],
        root=root,
    )

    manifest = compile_memory._load_manifest(request["generation_id"])
    journal = json.loads(
        json.dumps(compile_memory._load_journal(request["batch_id"]))
    )
    journal["operation_effects"][0]["after"]["identity"][0] = alternate_width
    real_load_journal = compile_memory._load_journal
    monkeypatch.setattr(
        compile_memory,
        "_load_journal",
        lambda batch_id, *args, **kwargs: (
            journal
            if batch_id == request["batch_id"]
            else real_load_journal(batch_id, *args, **kwargs)
        ),
    )

    replayed_receipt = compile_memory._revalidate_generation_effects(manifest)

    assert replayed_receipt["effects"][0]["after"]["identity"][0] == alternate_width


@pytest.mark.parametrize("mutation", ("delete", "replace", "strip-body"))
def test_invalidated_effect_retry_cannot_publish_an_empty_receipt(
    compile_env,
    mutation,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Invalid durable effects must rewind their source progress."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-14-replay-{mutation}.md",
        [_block("12:00:00", quote)],
    )
    slug = f"receipt-replay-{mutation}"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    original_request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        original_request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )["ok"]
    original = target.read_bytes()
    original_receipt = state["compiled_daily_receipts"][daily.name]
    marker = original_receipt["effects"][0]["marker"].encode()
    fingerprint = original_receipt["effects"][0]["fingerprint"].encode()

    if mutation == "delete":
        target.unlink()
    elif mutation == "replace":
        replacement = target.with_name(f"{slug}-replacement.md")
        replacement.write_bytes(original + b"\nFOREIGN REPLACEMENT\n")
        os.replace(replacement, target)
    else:
        target.write_bytes(marker + b"\n" + fingerprint + b"\n")

    assert compile_memory.trusted_compiled_daily_hashes(state, root=root) == {}
    args = argparse.Namespace(file=None, all=False, sdk_paths=None)
    assert compile_memory.select_dailies(args, state) == [daily]

    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    assert retry["pending"] is True
    assert retry["generation_id"] == original_request["generation_id"]
    assert retry["batch_id"] == original_request["batch_id"]
    assert quote in "\n".join(retry["source_blocks"])
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert daily.name not in state.get("compiled_daily_receipts", {})
    replay_boundary = state["compile_daily_replay_boundaries"][daily.name]
    assert replay_boundary["generation_id"] == original_request["generation_id"]
    assert replay_boundary["effects"]
    assert len(json.dumps(replay_boundary).encode("utf-8")) <= (
        compile_memory.MAX_COMPILE_RECEIPT_BYTES
    )

    replayed = compile_memory.apply_compile_batch(retry, _response(), False)
    if mutation == "delete":
        assert replayed["ok"] is True
        receipt = state["compiled_daily_receipts"][daily.name]
        assert receipt["effects"]
        assert receipt["targets"]
        assert target.read_bytes() != b""
        assert compile_memory.trusted_compiled_daily_hashes(state, root=root) == {
            daily.name: original_request["dailies"][0]["sha256"]
        }
    else:
        assert replayed["ok"] is False
        assert daily.name not in state.get("compiled_daily_hashes", {})
        assert daily.name not in state.get("compiled_daily_receipts", {})
        assert state.get("compile_index_pending") or compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )["pending"]


def test_replay_restores_missing_effect_without_duplicating_valid_sibling(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Replay repairs only invalid durable effects."
    sibling_quote = "Always preserve a valid sibling while replay repairs another effect."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-14-replay-sibling.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                (
                    "**Lessons / patterns**\n"
                    f"- {quote}\n"
                    f"- {sibling_quote}"
                ),
                synthetic_rule=False,
            )
        ],
    )
    first_slug = "receipt-replay-missing-effect"
    second_slug = "receipt-replay-valid-effect"
    first = root / "knowledge" / "notes" / f"{first_slug}.md"
    second = root / "knowledge" / "notes" / f"{second_slug}.md"
    original_request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        original_request,
        _response(
            _admission_operation(
                daily,
                first_slug,
                quote,
                summary="The first replay effect is independently durable.",
            ),
            _admission_operation(
                daily,
                second_slug,
                    sibling_quote,
                summary="The second replay effect remains independently durable.",
            ),
        ),
        False,
    )["ok"]
    second_before = second.read_bytes()
    second_marker = state["compiled_daily_receipts"][daily.name]["effects"][1][
        "marker"
    ].encode()
    first.unlink()

    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    replayed = compile_memory.apply_compile_batch(retry, _response(), False)

    assert replayed["ok"] is True
    assert first.is_file()
    assert second.read_bytes() == second_before
    assert second.read_bytes().count(second_marker) == 1
    assert len(state["compiled_daily_receipts"][daily.name]["effects"]) == 2


def test_missing_receipt_rewinds_checkpoint_and_rejects_empty_replay(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "A legacy completion without a receipt cannot retain source progress."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-14-replay-legacy.md",
        [_block("12:00:00", quote)],
    )
    slug = "receipt-replay-missing"
    original_request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        original_request,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )["ok"]
    state["compiled_daily_receipts"].pop(daily.name)

    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    assert retry["pending"] is True
    assert quote in "\n".join(retry["source_blocks"])
    replay_boundary = state["compile_daily_replay_boundaries"][daily.name]
    assert replay_boundary["generation_id"] is None
    assert replay_boundary["requires_nonempty"] is True
    result = compile_memory.apply_compile_batch(retry, _response(), False)
    assert result["ok"] is False
    assert daily.name not in state.get("compiled_daily_hashes", {})
    assert daily.name not in state.get("compiled_daily_receipts", {})
    assert "compile_index_pending" not in state
    assert daily.name not in state.get("compile_generation_active", {})
    assert daily.name not in state.get("compile_sdk_progress", {})


def test_missing_receipt_empty_replays_get_fresh_attempt_for_later_valid_plan(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "A rejected empty legacy replay must not pin later provider work."
    recovery_quote = "Always reserve a distinct citation for later recovered work."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-14-legacy-r3.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                (
                    "**Lessons / patterns**\n"
                    f"- {quote}\n"
                    f"- {recovery_quote}"
                ),
                synthetic_rule=False,
            )
        ],
    )
    original = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        original,
        _response(_admission_operation(daily, "legacy-original", quote)),
        False,
    )["ok"]
    state["compiled_daily_receipts"].pop(daily.name)
    rejected_ids = []

    for _restart in range(3):
        request = compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )
        rejected_ids.append((request["generation_id"], request["batch_id"]))
        rejected = compile_memory.apply_compile_batch(request, _response(), False)
        assert rejected["ok"] is False
        assert "compile_index_pending" not in state

    valid_request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    valid = compile_memory.apply_compile_batch(
        valid_request,
        _response(
            _admission_operation(
                daily,
                "legacy-recovered",
                recovery_quote,
                summary="A recovered legacy replay now has a durable effect.",
            )
        ),
        False,
    )

    assert len({*rejected_ids, (valid_request["generation_id"], valid_request["batch_id"])}) == 4
    assert valid["ok"] is True, valid
    assert (root / "knowledge" / "notes" / "legacy-recovered.md").is_file()
    receipt = state["compiled_daily_receipts"][daily.name]
    assert receipt["effects"]
    assert receipt["targets"]


def test_accepted_journal_replay_rechecks_live_create_duplicates(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "Accepted creates must remain unique when replay begins."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-14-replay-dedup.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "accepted-replay-target",
        quote,
        title="Replay Duplicate Identity",
    )
    response = _response(operation)
    plan, error = compile_memory._normalize_accepted_plan(
        response,
        [daily],
        request["source_blocks"],
    )
    assert error == ""
    assert plan is not None
    compile_memory._create_journal(request, response, plan)
    duplicate = root / "knowledge" / "notes" / "later-duplicate.md"
    _write_note(
        duplicate,
        "Replay Duplicate Identity",
        "A distinct summary created after durable journal acceptance.",
    )
    target = root / "knowledge" / "notes" / "accepted-replay-target.md"
    duplicate_before = duplicate.read_bytes()

    result = compile_memory.apply_compile_batch(request, _response(), False)

    assert result["ok"] is False
    assert result["status"] == "apply_failed"
    assert "matches active normalized title" in result["error"]
    assert not target.exists()
    assert duplicate.read_bytes() == duplicate_before
    journal = json.loads(
        (
            state_root
            / "run"
            / "compile-journal"
            / f"{request['batch_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["operation_states"] == ["pending"]


def test_oversized_publication_preserves_prior_state_bytes_and_actionable_journal(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, state_root, _state = compile_env
    state_dir = state_root / "run"
    state_file = state_dir / "state.json"
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", state_root / "logs")
    monkeypatch.setattr(compile_memory, "load_state", memory_state.load_state)
    monkeypatch.setattr(compile_memory, "update_state", memory_state.update_state)
    quote = "An oversized final receipt must preserve prior state bytes exactly."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-14-oversized-publication.md",
        [_block("12:00:00", quote)],
    )
    canonical = {}
    request = compile_memory.prepare_compile_request(
        [daily], canonical, prompt_char_budget=30_000
    )
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: False)
    first = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "oversized-publication", quote)),
        False,
    )
    assert first["status"] == "index_pending"
    prior = memory_state.load_state()
    prior["oversized_publication_padding"] = ""
    unpadded = json.dumps(prior, indent=2, ensure_ascii=False).encode("utf-8")
    assert len(unpadded) < 11_907
    prior["oversized_publication_padding"] = "x" * (11_907 - len(unpadded))
    memory_state.save_state(prior)
    before = state_file.read_bytes()
    assert len(before) == 11_907
    monkeypatch.setattr(memory_state, "MAX_STATE_JSON_CHARS", 12_607)
    monkeypatch.setattr(
        compile_memory,
        "rebuild_index",
        lambda: _rebuild_test_index(compile_memory),
    )

    result = compile_memory._resume_pending_index_if_any()

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "index_pending"
    after = state_file.read_bytes()
    assert after == before, (len(before), len(after))
    journal_path = (
        state_root / "run" / "compile-journal" / f"{request['batch_id']}.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "index_pending"
    assert journal["operation_states"] == ["applied"]


def test_prepare_reclaims_post_publication_crash_manifests_beyond_state_window(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_ACTIVE_MANIFESTS", 1)
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 1)
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 1)
    real_prune = compile_memory._prune_completed_manifests
    crash_after_publication = False

    def crash_or_reconcile(*args, **kwargs):
        nonlocal crash_after_publication
        if crash_after_publication:
            crash_after_publication = False
            raise SystemExit("injected post-publication crash")
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(
        compile_memory,
        "_prune_completed_manifests",
        crash_or_reconcile,
    )
    active_dir = state_root / "run" / "compile-manifests"

    for index in range(3):
        daily = _daily(
            root / "knowledge" / "daily" / f"2026-10-{22 + index}-post-publish.md",
            [_block("12:00:00", f"Post-publication crash generation {index}.")],
        )
        request = compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )
        assert len(list(active_dir.glob("*.json"))) == 1
        crash_after_publication = True

        with pytest.raises(SystemExit, match="post-publication crash"):
            compile_memory.apply_compile_batch(request, _response(), False)

        receipt = state["compiled_daily_receipts"][daily.name]
        assert receipt["generation_id"] == request["generation_id"]
        assert compile_memory.trusted_compiled_daily_hashes(state, root=root).get(
            daily.name
        ) == request["dailies"][0]["sha256"]
        assert len(list(active_dir.glob("*.json"))) == 1


def test_later_journaled_append_refreshes_prior_daily_effect_receipt(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    first_quote = "The original durable effect remains required after later updates."
    second_quote = "A later journaled update extends the same durable page."
    first_daily = _daily(
        root / "knowledge" / "daily" / "2026-10-15-receipt-first.md",
        [_block("12:00:00", first_quote)],
    )
    second_daily = _daily(
        root / "knowledge" / "daily" / "2026-10-16-receipt-second.md",
        [_block("12:00:00", second_quote)],
    )
    slug = "receipt-chain"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    first_request = compile_memory.prepare_compile_request(
        [first_daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        first_request,
        _response(_admission_operation(first_daily, slug, first_quote)),
        False,
    )["ok"]
    first_identity = state["compiled_daily_receipts"][first_daily.name]["targets"][
        0
    ]["current"]["identity"]

    second_request = compile_memory.prepare_compile_request(
        [second_daily], state, prompt_char_budget=30_000
    )
    update = _admission_operation(
        second_daily,
        slug,
        second_quote,
        action="update",
    )
    assert compile_memory.apply_compile_batch(
        second_request,
        _response(update),
        False,
    )["ok"]

    refreshed = state["compiled_daily_receipts"][first_daily.name]
    current = compile_memory._read_knowledge_page_snapshot(target)[1]
    args = argparse.Namespace(file=None, all=False, sdk_paths=None)
    assert refreshed["targets"][0]["current"] == current
    assert refreshed["targets"][0]["current"]["identity"] != first_identity
    assert compile_memory.select_dailies(args, state) == []


def test_new_journal_directory_parent_sync_failure_prevents_all_effects(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "first-dir")
    journal_dir = case["state_root"] / "run" / "compile-journal"
    assert not journal_dir.exists()
    real_sync = case["compile_memory"].sync_parent_directory_strict
    exchanges = []
    real_exchange = memory_state._exchange_expected_base_files

    def fail_new_directory_sync(path):
        if Path(path) == journal_dir:
            raise OSError("injected new journal directory parent sync failure")
        return real_sync(path)

    def count_exchange(*args, **kwargs):
        exchanges.append(1)
        return real_exchange(*args, **kwargs)

    monkeypatch.setattr(
        case["compile_memory"],
        "sync_parent_directory_strict",
        fail_new_directory_sync,
    )
    monkeypatch.setattr(memory_state, "_exchange_expected_base_files", count_exchange)

    result = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    assert result["ok"] is False
    assert result["status"] == "journal_error"
    assert exchanges == []
    assert case["target"].read_bytes() == case["original"]
    assert case["daily"].name not in case["state"].get(
        "compiled_daily_hashes", {}
    )
    assert not case["journal_path"].exists()


def test_restart_syncs_existing_journal_parent_chain_before_any_exchange(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "restart-parent-chain")
    journal_dir = case["state_root"] / "run" / "compile-journal"
    journal_dir.mkdir()
    fresh = importlib.reload(case["compile_memory"])
    state = case["state"]

    def load_state():
        return json.loads(json.dumps(state))

    def update_state(mutator):
        mutator(state)
        return state

    monkeypatch.setattr(fresh, "ROOT", case["root"])
    monkeypatch.setattr(fresh, "STATE_ROOT", case["state_root"])
    monkeypatch.setattr(fresh, "DAILY_DIR", case["root"] / "knowledge" / "daily")
    monkeypatch.setattr(fresh, "KNOWLEDGE", case["root"] / "knowledge" / "notes")
    monkeypatch.setattr(fresh, "INDEX", case["root"] / "knowledge" / "index.md")
    monkeypatch.setattr(fresh, "AGENTS", case["root"] / "AGENTS.md")
    monkeypatch.setattr(fresh, "LOG", case["root"] / "knowledge" / "log.md")
    monkeypatch.setattr(fresh, "load_state", load_state)
    monkeypatch.setattr(fresh, "update_state", update_state)
    monkeypatch.setattr(
        fresh,
        "rebuild_index",
        lambda: _rebuild_test_index(fresh),
    )
    real_sync = fresh.sync_parent_directory_strict
    sync_attempts = []
    exchanges = []
    real_exchange = memory_state._exchange_expected_base_files

    def fail_restarted_parent_sync(path):
        candidate = Path(path)
        sync_attempts.append(candidate)
        if candidate == journal_dir:
            raise OSError("injected restarted journal parent sync failure")
        return real_sync(path)

    def count_exchange(*args, **kwargs):
        exchanges.append(1)
        return real_exchange(*args, **kwargs)

    monkeypatch.setattr(fresh, "sync_parent_directory_strict", fail_restarted_parent_sync)
    monkeypatch.setattr(memory_state, "_exchange_expected_base_files", count_exchange)

    result = fresh.apply_compile_batch(case["request"], case["response"], False)

    assert result["ok"] is False
    assert result["status"] == "journal_error"
    assert journal_dir in sync_attempts
    assert exchanges == []
    assert case["target"].read_bytes() == case["original"]
    assert case["daily"].name not in state.get("compiled_daily_hashes", {})


def test_visible_journal_is_redurabilized_before_resume_mutation(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "resume-redurable")
    real_sync_file = case["compile_memory"].sync_file_strict
    real_exchange = memory_state._exchange_expected_base_files
    sync_attempts = 0
    exchanges = 0

    def fail_first_two_journal_syncs(path):
        nonlocal sync_attempts
        if Path(path) == case["journal_path"]:
            sync_attempts += 1
            if sync_attempts <= 2:
                raise OSError("injected journal file sync failure")
        return real_sync_file(path)

    def count_exchange(*args, **kwargs):
        nonlocal exchanges
        exchanges += 1
        return real_exchange(*args, **kwargs)

    monkeypatch.setattr(
        case["compile_memory"],
        "sync_file_strict",
        fail_first_two_journal_syncs,
    )
    monkeypatch.setattr(memory_state, "_exchange_expected_base_files", count_exchange)

    first = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )
    visible_journal = case["journal_path"].read_bytes()
    second = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    assert first["status"] == "journal_error"
    assert second["status"] == "journal_error"
    assert sync_attempts == 2
    assert exchanges == 0
    assert case["target"].read_bytes() == case["original"]
    assert case["journal_path"].read_bytes() == visible_journal
    assert case["daily"].name not in case["state"].get(
        "compiled_daily_hashes", {}
    )

    third = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    assert third["ok"] is True
    assert sync_attempts > 2
    assert exchanges > 0


def test_cleanup_displaced_replacement_race_preserves_foreign_bytes_and_hash(
    compile_env,
    monkeypatch,
):
    import memory_state

    case = _journaled_update_case(compile_env, "cleanup-unlink-race")
    real_retire = memory_state._retire_open_descriptor
    foreign = b"FOREIGN AFTER OWNERSHIP CHECK\n"
    raced_path = None

    def replace_after_check(path, descriptor, replacement):
        nonlocal raced_path
        candidate = Path(path)
        if raced_path is None and candidate.name.endswith(
            (".replacement", ".displaced", ".rejected")
        ):
            raced_path = candidate
            checked = candidate.with_name("checked-cleanup-displaced.md")
            racer = candidate.with_name("cleanup-displaced-racer.md")
            racer.write_bytes(foreign)
            os.replace(candidate, checked)
            os.replace(racer, candidate)
        return real_retire(path, descriptor, replacement)

    monkeypatch.setattr(memory_state, "_retire_open_descriptor", replace_after_check)

    result = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    assert raced_path is not None
    assert result["ok"] is False
    assert result["status"] == "apply_failed"
    assert raced_path.read_bytes() == foreign
    assert case["daily"].name not in case["state"].get(
        "compiled_daily_hashes", {}
    )


def test_direct_cleanup_replacement_race_preserves_foreign_bytes(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "direct-retire-race.md"
    target.write_text("admitted base\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    real_retire = memory_state._retire_open_descriptor
    foreign = b"DIRECT FOREIGN RETAINED\n"
    raced_path = None

    def replace_after_check(path, descriptor, replacement):
        nonlocal raced_path
        candidate = Path(path)
        if raced_path is None and candidate != target:
            raced_path = candidate
            checked = candidate.with_name("checked-direct-retire.md")
            racer = candidate.with_name("direct-retire-racer.md")
            racer.write_bytes(foreign)
            os.replace(candidate, checked)
            os.replace(racer, candidate)
        return real_retire(path, descriptor, replacement)

    monkeypatch.setattr(memory_state, "_retire_open_descriptor", replace_after_check)

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(memory_state.AtomicWriteRecoveryError):
            memory_state.conditional_atomic_write(
                target,
                "published replacement\n",
                expected,
            )

    assert raced_path is not None
    assert raced_path.read_bytes() == foreign


@pytest.mark.skipif(os.name != "posix", reason="POSIX retains displaced inodes")
def test_retirement_preserves_hard_link_created_after_snapshot(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "late-hard-link-race.md"
    original = b"admitted base must remain byte exact\n"
    target.write_bytes(original)
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    alias = target.with_name("late-hard-link-race-alias.md")
    real_retire = memory_state._retire_open_descriptor
    linked = False

    def link_after_check(path, descriptor, replacement):
        nonlocal linked
        candidate = Path(path)
        if not linked and candidate != target:
            _hard_link_or_skip(candidate, alias)
            linked = True
        return real_retire(path, descriptor, replacement)

    monkeypatch.setattr(memory_state, "_retire_open_descriptor", link_after_check)

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        memory_state.conditional_atomic_write(
            target,
            "published replacement\n",
            expected,
        )

    assert linked is True
    artifacts = list(target.parent.glob(f".{target.name}.*.replacement"))
    assert target.read_bytes() == b"published replacement\n"
    assert alias.read_bytes() == original
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == original
    assert os.path.samefile(alias, artifacts[0])


@pytest.mark.skipif(os.name != "posix", reason="POSIX retains displaced inodes")
def test_journal_effect_records_exact_retained_displaced_artifact(
    compile_env,
):
    case = _journaled_update_case(compile_env, "retained-effect")

    result = case["compile_memory"].apply_compile_batch(
        case["request"], case["response"], False
    )

    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    effect = journal["operation_effects"][0]
    retained = effect["retained_artifact"]
    artifact = case["target"].with_name(retained["path"])
    snapshot = retained["snapshot"]
    metadata = artifact.stat()
    assert result["ok"] is True
    assert effect["version"] == 2
    assert artifact.read_bytes() == case["original"]
    assert snapshot["identity"] == [metadata.st_dev, metadata.st_ino, stat.S_IFREG]
    assert snapshot["sha256"] == hashlib.sha256(case["original"]).hexdigest()
    assert snapshot["size"] == len(case["original"])
    assert snapshot["nlink"] >= 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX retains displaced inodes")
@pytest.mark.parametrize(
    ("scope", "measure"),
    (
        ("per-target", "count"),
        ("per-target", "byte"),
        ("global", "count"),
        ("global", "byte"),
    ),
)
def test_retained_artifact_capacity_refuses_before_exchange(
    compile_env,
    monkeypatch,
    scope,
    measure,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "retained-capacity.md"
    original = b"prospective retained base\n"
    target.write_bytes(original)
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    inventory_target = (
        target
        if scope == "per-target"
        else target.with_name("other-retained-target.md")
    )
    artifact = inventory_target.with_name(
        f".{inventory_target.name}.{'1' * 32}.displaced"
    )
    retained = b"existing retained artifact\n"
    artifact.write_bytes(retained)
    monkeypatch.setattr(
        memory_state,
        "MAX_RETAINED_CONDITIONAL_ARTIFACTS_PER_TARGET",
        100,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_PER_TARGET",
        10_000,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "MAX_RETAINED_CONDITIONAL_ARTIFACTS_GLOBAL",
        100,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_GLOBAL",
        10_000,
        raising=False,
    )
    constant = {
        ("per-target", "count"): "MAX_RETAINED_CONDITIONAL_ARTIFACTS_PER_TARGET",
        ("per-target", "byte"): (
            "MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_PER_TARGET"
        ),
        ("global", "count"): "MAX_RETAINED_CONDITIONAL_ARTIFACTS_GLOBAL",
        ("global", "byte"): "MAX_RETAINED_CONDITIONAL_ARTIFACT_BYTES_GLOBAL",
    }[(scope, measure)]
    limit = 1 if measure == "count" else len(retained) + len(original) - 1
    monkeypatch.setattr(memory_state, constant, limit, raising=False)
    exchanges = 0
    real_exchange = memory_state._exchange_expected_base_files

    def reject_exchange(*args, **kwargs):
        nonlocal exchanges
        exchanges += 1
        return real_exchange(*args, **kwargs)

    monkeypatch.setattr(
        memory_state,
        "_exchange_expected_base_files",
        reject_exchange,
    )

    before = {path: path.read_bytes() for path in target.parent.iterdir()}
    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(OSError, match=rf"{scope} retained artifact {measure} limit"):
            memory_state.conditional_atomic_write(
                target,
                "replacement must not publish\n",
                expected,
            )

    assert exchanges == 0
    assert target.read_bytes() == original
    assert {path: path.read_bytes() for path in target.parent.iterdir()} == before


def test_cleanup_revalidates_full_snapshot_for_same_inode_mutation(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "same-inode-retire-race.md"
    target.write_bytes(b"admitted base\n")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    real_unlink = memory_state._unlink_owned_bound_file
    foreign = b"FOREIGN BYTES\n"
    raced_path = None

    def mutate_same_inode_before_unlink(path, dir_fd, expected_snapshot):
        nonlocal raced_path
        candidate = Path(path)
        if raced_path is None and candidate != target:
            raced_path = candidate
            flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = (
                os.open(candidate, flags)
                if dir_fd is None
                else os.open(candidate.name, flags, dir_fd=dir_fd)
            )
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                assert os.write(descriptor, foreign) == len(foreign)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return real_unlink(path, dir_fd, expected_snapshot)

    monkeypatch.setattr(
        memory_state,
        "_unlink_owned_bound_file",
        mutate_same_inode_before_unlink,
    )

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(memory_state.AtomicWriteRecoveryError):
            memory_state.conditional_atomic_write(
                target,
                "published replacement\n",
                expected,
            )

    assert raced_path is not None
    assert raced_path.read_bytes() == foreign


@pytest.mark.skipif(os.name != "posix", reason="POSIX retains displaced inodes")
def test_repeated_success_retains_each_displaced_inode(compile_env):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "retained-history.md"
    target.write_text("base zero\n", encoding="utf-8")

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        for index in range(20):
            expected = compile_memory._read_knowledge_page_snapshot(target)[1]
            memory_state.conditional_atomic_write(
                target,
                f"base {index + 1}\n",
                expected,
            )

    artifacts = list(target.parent.glob(f".{target.name}.*"))
    assert len(artifacts) == 20
    assert {path.read_bytes() for path in artifacts} == {b"base zero\n"} | {
        f"base {index}\n".encode() for index in range(1, 20)
    }
    assert target.read_text(encoding="utf-8") == "base 20\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX retains displaced inodes")
def test_prepared_success_retains_each_displaced_inode(compile_env):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "prepared-retained-history.md"
    target.write_text("prepared zero\n", encoding="utf-8")

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        for index in range(20):
            expected = compile_memory._read_knowledge_page_snapshot(target)[1]
            reservation = memory_state.prepare_conditional_atomic_write(
                target,
                f"prepared {index + 1}\n",
                expected,
                f"prepared-reuse-{index}",
            )
            memory_state.conditional_atomic_write(
                target,
                reservation,
                persist_recovery=lambda _state: None,
            )
            reservation["status"] = "cleanup_pending"
            memory_state.finalize_conditional_atomic_write(
                target,
                reservation,
                persist_recovery=lambda _state: None,
            )

    artifacts = list(target.parent.glob(f".{target.name}.*"))
    assert len(artifacts) == 20
    assert {path.read_bytes() for path in artifacts} == {b"prepared zero\n"} | {
        f"prepared {index}\n".encode() for index in range(1, 20)
    }
    assert target.read_text(encoding="utf-8") == "prepared 20\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX retains displaced inodes")
def test_retained_displaced_inodes_refuse_capacity_overflow(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, _state_root, _state = compile_env
    target = root / "knowledge" / "notes" / "resolved-cap.md"
    target.write_text("admitted base\n", encoding="utf-8")
    expected = compile_memory._read_knowledge_page_snapshot(target)[1]
    monkeypatch.setattr(
        memory_state,
        "MAX_RETAINED_CONDITIONAL_ARTIFACTS_PER_TARGET",
        2,
    )
    for index in range(2):
        candidate = target.with_name(
            f".{target.name}.{index:032x}.displaced"
        )
        candidate.write_bytes(f"retained base {index}\n".encode())
    before = {path: path.read_bytes() for path in target.parent.glob(f".{target.name}.*")}

    with memory_state.bind_atomic_writes_to_directory(target.parent):
        with pytest.raises(
            OSError,
            match="per-target retained artifact count limit",
        ):
            memory_state.conditional_atomic_write(
                target,
                "rejected replacement\n",
                expected,
            )

    assert target.read_text(encoding="utf-8") == "admitted base\n"
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("tag", ("script", "style", "pre", "textarea"))
@pytest.mark.parametrize(
    ("template", "visible_words"),
    (
        ("<{tag}\n{hidden}\n</{tag}>\nvisible", 1),
        ("- <{tag}\n  {hidden}\n  </{tag}>\n- visible", 1),
        ("> <{tag}\n> {hidden}\n> </{tag}>\nvisible", 1),
        ("- <{tag}\n  {hidden}\n- visible", 1),
        ("> <{tag}\n> {hidden}\nvisible", 1),
    ),
    ids=("root", "list", "blockquote", "list-boundary", "quote-boundary"),
)
def test_commonmark_type1_incomplete_openers_are_container_scoped(
    tag,
    template,
    visible_words,
):
    import compile_memory

    value = template.format(tag=tag, hidden=_word_body(149))

    assert compile_memory._body_word_count(value) == visible_words


@pytest.mark.parametrize(
    "spoof",
    (
        "```md\n**Lessons / patterns**\n```",
        "    **Lessons / patterns**",
        "<!--\n**Lessons / patterns**\n-->",
        "> **Lessons / patterns**",
        "- **Lessons / patterns**",
        r"\**Lessons / patterns**",
    ),
    ids=("fenced", "indented", "raw-html", "blockquote", "list", "escaped"),
)
def test_durable_section_heading_spoofs_do_not_validate_evidence(
    compile_env,
    spoof,
):
    compile_memory, root, _state_root, _state = compile_env
    quote = "spoofed durable evidence"
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-21-heading-spoof.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"{spoof}\n\n- {quote}\n\n"
                "**Lessons / patterns**\n"
                "- Always preserve a real durable sibling before validation.",
            )
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    plan, error = compile_memory._normalize_accepted_plan(
        _response(_admission_operation(daily, "heading-spoof", quote)),
        [daily],
        source_blocks,
    )

    assert plan is None
    assert "durable section" in error


@pytest.mark.parametrize(
    ("case_id", "slug", "body"),
    (
        (
            "list",
            "list-continuation-heading",
            "- wrapper\n"
            "  **Lessons / patterns**\n"
            "  - plain audit status only",
        ),
        (
            "tab-list",
            "tab-list-continuation-heading",
            "- wrapper\n"
            "\t**Lessons / patterns**\n"
            "\t- plain audit status only",
        ),
        (
            "tab-code",
            "tab-code-heading",
            "\t**Lessons / patterns**\n"
            "\tplain audit status only",
        ),
        (
            "quote",
            "blockquote-continuation-heading",
            "> wrapper\n"
            "**Lessons / patterns**\n"
            "plain audit status only",
        ),
    ),
)
def test_nested_durable_heading_rejects_plan_before_journal(
    compile_env,
    case_id,
    slug,
    body,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-21-{case_id}.md",
        [_generated_block("12:00:00", root, body)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert request == {"pending": False}
    assert not (root / "knowledge" / "notes" / f"{slug}.md").exists()
    assert not (state_root / "run" / "compile-journal").exists()


def test_tilde_fence_info_string_may_contain_backticks(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "tilde-fenced spoofed evidence"
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-21-tilde-fence.md",
        [
            _generated_block(
                "12:00:00",
                root,
                "~~~ markdown`variant\n"
                "**Lessons / patterns**\n"
                f"- {quote}\n"
                "~~~\n\n"
                "**Lessons / patterns**\n"
                "- Always keep fenced content outside durable evidence.",
            )
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    plan, error = compile_memory._normalize_accepted_plan(
        _response(_admission_operation(daily, "tilde-fence", quote)),
        [daily],
        source_blocks,
    )

    assert plan is None
    assert "not an exact source citation" in error


def test_real_durable_section_heading_validates_evidence(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    quote = "Always validate real durable evidence before publication."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-21-real-heading.md",
        [
            _generated_block(
                "12:00:00",
                root,
                f"**Lessons / patterns**\n\n- {quote}",
            )
        ],
    )
    source_blocks = compile_memory.extract_meaningful_blocks(
        daily.read_text(encoding="utf-8")
    )

    plan, error = compile_memory._normalize_accepted_plan(
        _response(_admission_operation(daily, "real-heading", quote)),
        [daily],
        source_blocks,
    )

    assert error == ""
    assert plan is not None


def test_unresolved_reference_labels_count_and_reject_oversized_create(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Unresolved reference labels remain visible rendered words."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-14-unresolved-reference.md",
        [_block("12:00:00", quote)],
    )
    body = " ".join(
        f"[visible{index}][also{index}]" for index in range(250)
    )
    assert compile_memory._body_word_count(body) == 500
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(
        daily,
        "unresolved-reference",
        quote,
        body=body,
    )

    result = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "150-400 words" in result["error"]


def test_oversized_daily_snapshot_is_rejected_before_manifest(compile_env):
    compile_memory, root, state_root, state = compile_env
    daily = root / "knowledge" / "daily" / "2026-10-15-oversized.md"
    daily.write_bytes(
        b"# Daily\n\n## [12:00:00] session-end | session\n"
        + b"x" * (compile_memory.MAX_DAILY_SNAPSHOT_BYTES + 1)
    )

    with pytest.raises(compile_memory.CompilePreparationError, match="daily snapshot"):
        compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )

    assert not list((state_root / "run" / "compile-manifests").glob("*.json"))
    assert daily.name not in state.get("compiled_daily_hashes", {})


def test_manifest_pruning_bounds_count_and_aggregate_bytes_without_active_loss(
    compile_env,
    monkeypatch,
):
    compile_memory, _root, state_root, state = compile_env
    directory = state_root / "run" / "compile-manifests"
    directory.mkdir(parents=True)
    active_id = "a" * 64
    completed_ids = [f"{index:064x}" for index in range(1, 4)]
    payload_size = 2 * 1024 * 1024
    for generation_id in [active_id, *completed_ids]:
        (directory / f"{generation_id}.json").write_bytes(b"x" * payload_size)
    state["compile_generation_active"] = {
        "active.md": {"generation_id": active_id, "source_sha256": "b" * 64}
    }
    state["compile_generation_completed"] = list(completed_ids)
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_MANIFESTS", 10)
    monkeypatch.setattr(
        compile_memory,
        "MAX_RETAINED_MANIFEST_BYTES",
        5 * 1024 * 1024,
        raising=False,
    )

    compile_memory._prune_completed_manifests()

    retained = sorted(directory.glob("*.json"))
    assert (directory / f"{active_id}.json") in retained
    assert sum(path.stat().st_size for path in retained) <= 5 * 1024 * 1024
    assert not (directory / f"{completed_ids[0]}.json").exists()
    assert state["compile_generation_completed"] == completed_ids[-1:]


@pytest.mark.parametrize("limit", ("count", "bytes"))
def test_new_manifest_refuses_exhausted_active_capacity(
    compile_env,
    monkeypatch,
    limit,
):
    compile_memory, root, state_root, state = compile_env
    directory = state_root / "run" / "compile-manifests"
    directory.mkdir(parents=True)
    active_ids = [f"{index:064x}" for index in range(1, 3)]
    for generation_id in active_ids:
        (directory / f"{generation_id}.json").write_bytes(b"x" * 128)
    state["compile_generation_active"] = {
        f"active-{index}.md": {
            "generation_id": generation_id,
            "source_sha256": "f" * 64,
        }
        for index, generation_id in enumerate(active_ids)
    }
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-10-15-active-{limit}.md",
        [_block("12:00:00", f"active manifest {limit} capacity")],
    )
    monkeypatch.setattr(
        compile_memory,
        "MAX_ACTIVE_MANIFESTS",
        len(active_ids) if limit == "count" else len(active_ids) + 10,
    )
    monkeypatch.setattr(
        compile_memory,
        "MAX_ACTIVE_MANIFEST_BYTES",
        256 if limit == "bytes" else 1024 * 1024,
    )
    before = set(directory.glob("*.json"))

    with pytest.raises(
        compile_memory.CompilePreparationError,
        match="active compile generation manifest count or byte limit",
    ):
        compile_memory._create_generation_manifest(daily, 30_000, state)

    assert set(directory.glob("*.json")) == before
    assert daily.name not in state["compile_generation_active"]


def test_new_manifest_counts_orphan_files_against_active_capacity(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    directory = state_root / "run" / "compile-manifests"
    directory.mkdir(parents=True)
    monkeypatch.setattr(compile_memory, "MAX_ACTIVE_MANIFESTS", 2)
    for index in range(2):
        (directory / f"{index + 1:064x}.json").write_bytes(b"{}")
    before = {path: path.read_bytes() for path in directory.glob("*.json")}
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-15-orphan-capacity.md",
        [_block("12:00:00", "Orphan manifests consume admission capacity.")],
    )

    with pytest.raises(
        compile_memory.CompilePreparationError,
        match="active compile generation manifest count or byte limit",
    ):
        compile_memory._create_generation_manifest(daily, 30_000, state)

    assert {path: path.read_bytes() for path in directory.glob("*.json")} == before
    assert daily.name not in state.get("compile_generation_active", {})


def test_narrow_prepare_cannot_clear_incomplete_canonical_audit_wave(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    dailies = [
        _daily(
            root / "knowledge" / "daily" / f"2026-10-{16 + index}.md",
            [_block("12:00:00", f"subset wave evidence {index}")],
        )
        for index in range(2)
    ]
    first = compile_memory.prepare_compile_request(
        dailies, state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        first, _response(audit={"stubs": 2}), False
    )["ok"]
    first_name = Path(first["dailies"][0]["path"]).name
    second_name = next(path.name for path in dailies if path.name != first_name)
    canonical = json.loads(json.dumps(state["compile_sdk_wave"]))

    narrow = compile_memory.prepare_compile_request(
        [next(path for path in dailies if path.name == first_name)],
        state,
        prompt_char_budget=30_000,
    )

    assert narrow == {"pending": False}
    surviving = state["compile_sdk_wave"]
    assert surviving["status"] == "active"
    assert surviving["expected"] == canonical["expected"]
    assert surviving["completed"] == canonical["completed"]
    assert surviving["daily_audits"] == canonical["daily_audits"]
    resumed = compile_memory.prepare_compile_request(
        [next(path for path in dailies if path.name == second_name)],
        state,
        prompt_char_budget=30_000,
    )
    assert Path(resumed["dailies"][0]["path"]).name == second_name


@pytest.mark.parametrize(
    "forbidden",
    ("\x00", "\x01", "\x1f", "\x7f", "\x85", "\x9f", "\ufffe", "\uffff"),
    ids=("nul", "c0-low", "c0-high", "del", "c1-nel", "c1-high", "fffe", "ffff"),
)
@pytest.mark.parametrize(
    "location",
    (
        "title",
        "summary",
        "body_markdown",
        "related",
        "daily_date",
        "timestamp",
        "quoted_text",
        "claim",
    ),
)
def test_provider_yaml_forbidden_characters_reject_before_journal(
    compile_env,
    forbidden,
    location,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Provider metadata must remain safe YAML and UTF-8."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-18-control.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    operation = _admission_operation(daily, "yaml-control", quote)
    if location in {"title", "summary"}:
        operation[location] += forbidden
    elif location == "body_markdown":
        operation[location] += forbidden
    elif location == "related":
        operation[location] = [f"[[safe{forbidden}]]"]
    else:
        operation["evidence"][0][location] += forbidden

    result = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    expected_error = (
        "invalid Unicode scalar"
        if compile_memory._is_unicode_noncharacter(ord(forbidden))
        else "forbidden YAML character"
    )
    assert expected_error in result["error"]
    assert not (root / "knowledge" / "notes" / "yaml-control.md").exists()


@pytest.mark.parametrize(
    "noncharacter",
    ("\ufdd0", "\ufffe", "\U0001ffff", "\U0010ffff"),
    ids=("fdd0", "bmp-plane-end", "plane-1-end", "plane-16-end"),
)
def test_noncharacter_source_is_rejected_before_manifest_write(
    compile_env,
    noncharacter,
):
    compile_memory, root, state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-18-source-noncharacter.md",
        [_block("12:00:00", f"Always reject {noncharacter} before persistence.")],
    )

    with pytest.raises(
        compile_memory.CompilePreparationError,
        match="invalid Unicode scalar",
    ):
        compile_memory.prepare_compile_request(
            [daily],
            state,
            prompt_char_budget=30_000,
        )

    manifest_dir = state_root / "run" / "compile-manifests"
    assert not manifest_dir.exists() or list(manifest_dir.glob("*.json")) == []
    assert compile_memory.load_state() == {}
    assert state == {}


def test_manifest_encoding_round_trips_through_strict_reader(compile_env):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-18-source-round-trip.md",
        [_block("12:00:00", "Always preserve valid Unicode: caf\u00e9 \U0001f642.")],
    )
    request = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=30_000,
    )
    manifest = compile_memory._load_manifest(request["generation_id"])
    encoded = compile_memory._manifest_encoded(manifest)

    assert compile_memory.decode_json_object_strict(
        encoded,
        max_bytes=compile_memory.MAX_GENERATION_MANIFEST_BYTES,
    ) == manifest


def test_rendered_accepted_metadata_parses_with_pyyaml(compile_env):
    import yaml

    compile_memory, root, _state_root, state = compile_env
    quote = "Accepted metadata is valid YAML under a reference parser."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-10-19-pyyaml.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    title = 'Quoted: "Title" \\ path # safe'
    summary = 'Summary: "quoted" \\ value # safe'
    operation = _admission_operation(
        daily,
        "pyyaml-valid",
        quote,
        title=title,
        summary=summary,
    )

    result = compile_memory.apply_compile_batch(
        request, _response(operation), False
    )

    assert result["ok"] is True
    content = (root / "knowledge" / "notes" / "pyyaml-valid.md").read_text(
        encoding="utf-8"
    )
    frontmatter = content.split("---", 2)[1]
    parsed = yaml.safe_load(frontmatter)
    assert parsed["title"] == title
    assert parsed["description"] == summary


def _legacy_v2_manifest(compile_memory, daily: Path, *, budget: int = 30_000):
    source_bytes, source_text = compile_memory._read_daily_snapshot(daily)
    return compile_memory._derive_manifest_v2(
        daily,
        source_text,
        budget,
        compile_memory._compile_context_snapshot(budget),
        None,
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


def test_shipped_v2_fixture_loads_without_rebuilding_its_prompt(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    fixture_path = Path(__file__).parent / "fixtures" / "compile-v2-93be6b8.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    manifest = fixture["manifest"]
    journal = fixture["journal"]
    daily = root / manifest["daily"]["path"]
    daily.write_text(fixture["source_utf8"], encoding="utf-8")
    manifest_path = (
        state_root
        / "run"
        / "compile-manifests"
        / f"{manifest['generation_id']}.json"
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    journal_path = (
        state_root / "run" / "compile-journal" / f"{journal['batch_id']}.json"
    )
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text(
        json.dumps(journal, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    regenerated = compile_memory._derive_manifest_v2(
        daily,
        manifest["source_utf8"],
        manifest["prompt_char_budget"],
        manifest["context"],
        manifest["base_checkpoint"],
    )
    assert regenerated["batches"][0]["prompt"] != manifest["batches"][0]["prompt"]

    def reject_prompt_rebuild(*_args, **_kwargs):
        pytest.fail("persisted v2 validation must not rebuild a current prompt")

    monkeypatch.setattr(compile_memory, "_derive_manifest_v2", reject_prompt_rebuild)

    assert compile_memory._load_manifest(manifest["generation_id"]) == manifest
    assert compile_memory._load_journal(journal["batch_id"]) == journal
    state["compile_generation_active"] = {
        daily.name: {
            "generation_id": manifest["generation_id"],
            "source_sha256": manifest["daily"]["sha256"],
        }
    }

    assert compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    ) == {"pending": False}
    assert not (root / "knowledge" / "notes" / "golden-v2-substring.md").exists()
    assert not manifest_path.exists()
    assert not journal_path.exists()
    assert list(
        (state_root / "run" / "retired-manifests").glob(
            f"{manifest['generation_id']}.*.json"
        )
    )
    assert list(
        (state_root / "run" / "retired-journals").glob(
            f"{journal['batch_id']}.*.json"
        )
    )


def test_shipped_scoped_v2_fixture_preserves_windows_root_and_migrates(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    fixture_path = (
        Path(__file__).parent / "fixtures" / "compile-v2-scoped-93be6b8.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    manifest = fixture["manifest"]
    journal = fixture["journal"]
    daily = root / manifest["daily"]["path"]
    daily.write_bytes(fixture["source_utf8"].encode("utf-8"))

    def reject_current_daily_record(*_args, **_kwargs):
        pytest.fail("legacy v2 validation must not use the current DailyRecord parser")

    source = fixture["source_utf8"].replace("\r\n", "\n")
    assert 'Project root JSON: "D:/projects/alpha"' in manifest["daily_blocks"][0]

    manifest_path = (
        state_root
        / "run"
        / "compile-manifests"
        / f"{manifest['generation_id']}.json"
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    journal_path = (
        state_root / "run" / "compile-journal" / f"{journal['batch_id']}.json"
    )
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text(
        json.dumps(journal, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with monkeypatch.context() as legacy_patch:
        legacy_patch.setattr(
            compile_memory,
            "parse_daily_records",
            reject_current_daily_record,
        )
        legacy_patch.setattr(
            compile_memory,
            "render_daily_record_for_legacy_compile",
            reject_current_daily_record,
            raising=False,
        )
        assert compile_memory._extract_legacy_manifest_blocks(source) == manifest[
            "daily_blocks"
        ]
        assert compile_memory._load_manifest(manifest["generation_id"]) == manifest
        assert compile_memory._load_journal(journal["batch_id"]) == journal
    state["compile_generation_active"] = {
        daily.name: {
            "generation_id": manifest["generation_id"],
            "source_sha256": manifest["daily"]["sha256"],
        }
    }

    assert compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    ) == {"pending": False}
    assert not (root / "knowledge" / "notes" / "golden-v2-scoped.md").exists()
    assert not manifest_path.exists()
    assert not journal_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (("batch_index", False), ("batch_count", True)),
)
def test_shipped_v2_rejects_boolean_batch_position_drift(
    compile_env,
    field,
    value,
):
    compile_memory, _root, state_root, _state = compile_env
    fixture_path = Path(__file__).parent / "fixtures" / "compile-v2-93be6b8.json"
    manifest = json.loads(fixture_path.read_text(encoding="utf-8"))["manifest"]
    manifest["batches"][0][field] = value
    manifest["manifest_sha256"] = compile_memory._manifest_digest(manifest)
    manifest_path = (
        state_root
        / "run"
        / "compile-manifests"
        / f"{manifest['generation_id']}.json"
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(
        compile_memory.CompileManifestError,
        match="legacy manifest batch fields",
    ):
        compile_memory._load_manifest(manifest["generation_id"])


def _activate_legacy_manifest(compile_memory, state: dict, daily: Path, manifest: dict):
    compile_memory._write_new_manifest(manifest)
    state["compile_generation_active"] = {
        daily.name: {
            "generation_id": manifest["generation_id"],
            "source_sha256": manifest["daily"]["sha256"],
        }
    }
    return compile_memory._request_from_manifest(
        manifest,
        manifest["batch_ids"][0],
    )


def _legacy_accepted_operation(
    daily: Path,
    slug: str,
    quote: str,
    root: Path,
    *,
    action: str = "create",
    expected_target: dict | None = None,
) -> dict:
    operation = _admission_operation(
        daily,
        slug,
        quote,
        action=action,
    )
    operation["evidence"][0]["quoted_text"] = quote
    operation.update(
        {
            "project_slug": _SYNTHETIC_PROJECT_SLUG,
            "project_root": str(root.resolve()),
            "_rendered_at": "2026-08-02T12:00:00",
            "_rendered_date": "2026-08-02",
        }
    )
    if expected_target is not None:
        operation["_expected_target"] = json.loads(json.dumps(expected_target))
    return operation


def _shipped_v1_operation(daily: Path, slug: str, quote: str) -> dict:
    operation = _admission_operation(daily, slug, quote)
    operation["evidence"][0]["quoted_text"] = quote
    operation.update(
        {
            "_rendered_at": "2026-08-02T12:00:00",
            "_rendered_date": "2026-08-02",
        }
    )
    return operation


def _render_shipped_v1_create(
    compile_memory,
    operation: dict,
    marker: str,
    fingerprint: str,
) -> str:
    title = operation["title"]
    summary = operation["summary"]
    category = operation["category"]
    evidence = "\n".join(
        f"- `knowledge/daily/{item['daily_date']}.md [{item['timestamp']}]` — "
        f"{item['claim']}"
        for item in operation["evidence"]
    )
    return (
        "---\n"
        f"type: {compile_memory.CATEGORY_SINGULAR[category]}\n"
        f'title: "{title}"\n'
        f'description: "{summary}"\n'
        f"timestamp: {operation['_rendered_at']}\n"
        "confidence: medium\n"
        "source_authority: ai-derived\n"
        "---\n\n"
        f"# {title}\n\n"
        f"One-sentence summary: {summary}\n\n"
        f"## {operation['body_section']}\n{operation['body_markdown']}\n\n"
        f"## Evidence\n{evidence}\n\n"
        f"{marker}\n{fingerprint}\n"
    )


def _write_legacy_v1_journal(
    compile_memory,
    request: dict,
    operations: list[dict],
    *,
    states: list[str] | None = None,
    recoveries: list[dict | None] | None = None,
    effects: list[dict | None] | None = None,
    status: str = "accepted",
) -> dict:
    accepted = {
        "operations": json.loads(json.dumps(operations)),
        "audit": {
            "verified": len(operations),
            "dedup": 0,
            "stubs": 0,
            "contradictions": 0,
            "rejected": 0,
        },
        "response_sha256": "a" * 64,
        "source": json.loads(json.dumps(request["dailies"])),
        "source_blocks": list(request["source_blocks"]),
        "batch_ids": list(request["batch_ids"]),
        "generation_id": request["generation_id"],
        "layout_sha256": request["layout_sha256"],
    }
    journal = {
        "version": 1,
        "batch_id": request["batch_id"],
        "accepted": accepted,
        "accepted_sha256": compile_memory._canonical_digest(accepted),
        "operation_states": states or ["pending"] * len(operations),
        "operation_recovery": recoveries or [None] * len(operations),
        "operation_effects": effects or [None] * len(operations),
        "status": status,
    }
    compile_memory._write_journal(journal)
    return journal


def _install_golden_v1_scoped_update(
    compile_memory,
    root: Path,
    state: dict,
    *,
    malformed: str | None = None,
) -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "compile-v2-93be6b8.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["provenance"]["commit"] == "93be6b8"
    manifest = fixture["manifest"]
    journal = fixture["journal"]
    daily = root / manifest["daily"]["path"]
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_bytes(fixture["source_utf8"].encode("utf-8"))
    compile_memory._write_new_manifest(manifest)
    state["compile_generation_active"] = {
        daily.name: {
            "generation_id": manifest["generation_id"],
            "source_sha256": manifest["daily"]["sha256"],
        }
    }

    target = root / "knowledge" / "notes" / "golden-v1-scoped-update.md"
    _write_note(
        target,
        "Golden V1 Scoped Update",
        "The scoped base predates the shipped unscoped update.",
        project_slug="golden-project",
        project_root=root,
    )
    before = compile_memory._read_knowledge_page_snapshot(target)
    assert before is not None
    operation = json.loads(json.dumps(journal["accepted"]["operations"][0]))
    operation.update(
        {
            "action": "update",
            "slug": target.stem,
            "title": "Golden V1 Scoped Update",
            "summary": "The shipped unscoped update was already applied exactly once.",
            "_expected_target": json.loads(json.dumps(before[1])),
        }
    )
    marker = compile_memory._operation_marker(
        {"batch_id": journal["batch_id"]},
        0,
        operation,
    )
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target.write_bytes(
        compile_memory._render_operation_result(
            operation,
            before[0],
            marker,
            fingerprint,
        ).encode("utf-8"),
    )
    after = compile_memory._read_knowledge_page_snapshot(target)
    assert after is not None
    effect = {
        "version": 2,
        "target": target.name,
        "before": json.loads(json.dumps(before[1])),
        "after": json.loads(json.dumps(after[1])),
        "retained_artifact": None,
    }
    if malformed == "effect-extra":
        effect["unexpected"] = True
    elif malformed == "version":
        effect["version"] = 1
    elif malformed == "target":
        effect["target"] = "different-target.md"
    elif malformed == "before-extra":
        effect["before"]["unexpected"] = True
    elif malformed == "after-extra":
        effect["after"]["unexpected"] = True
    elif malformed == "after-missing":
        effect["after"].pop("mode")
    elif malformed == "identity-shape":
        effect["after"]["identity"] = effect["after"]["identity"][:2]
    elif malformed == "identity-bool":
        effect["after"]["identity"][0] = True
    elif malformed == "sha256":
        effect["after"]["sha256"] = "z" * 64
    elif malformed == "size-bool":
        effect["after"]["size"] = True
    elif malformed == "mode-bool":
        effect["after"]["mode"] = True
    elif malformed == "file-attributes-bool":
        effect["after"]["file_attributes"] = True
    elif malformed == "nlink-zero":
        effect["after"]["nlink"] = 0
    elif malformed == "retained-artifact":
        effect["retained_artifact"] = {}
    elif malformed is not None:
        raise AssertionError(f"unknown malformed golden effect case: {malformed}")

    journal["accepted"]["operations"] = [operation]
    journal["accepted_sha256"] = compile_memory._canonical_digest(journal["accepted"])
    journal["operation_states"] = ["applied"]
    journal["operation_recovery"] = [None]
    journal["operation_effects"] = [effect]
    journal["status"] = "complete"
    compile_memory._write_journal(journal)
    return {
        "daily": daily,
        "manifest": manifest,
        "journal": journal,
        "target": target,
        "before": before[1],
        "after": after[1],
        "marker": marker,
        "fingerprint": fingerprint,
    }


def _retire_legacy_generation(compile_memory, manifest: dict, request: dict) -> None:
    with compile_memory._bound_journal_directory(create=False) as bound:
        name = f"{request['batch_id']}.json"
        admitted = compile_memory._journal_file_metadata(bound, name)
        compile_memory._retire_journal_file(bound, name, admitted)
    with compile_memory._bound_manifest_directory(create=False) as bound:
        generation_id = manifest["generation_id"]
        admitted = compile_memory._manifest_file_metadata(
            bound,
            f"{generation_id}.json",
        )
        compile_memory._retire_manifest_file(bound, generation_id, admitted)


def test_new_manifest_and_journal_versions_bind_current_admission(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Always validate the canonical source before applying a persisted plan."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-01-current-schema.md",
        [_block("12:00:00", quote)],
    )

    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    manifest = compile_memory._load_manifest(request["generation_id"])
    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "current-schema", quote)),
        False,
    )
    journal = compile_memory._load_journal(request["batch_id"])

    assert result["ok"] is True
    assert manifest["version"] == 3
    assert journal["version"] == 2


def test_legacy_v2_manifest_derivation_is_byte_exact_to_shipped_full_record(
    compile_env,
):
    compile_memory, root, _state_root, _state = compile_env
    status = "Status: implementation complete; twelve files changed."
    lesson = "Always preserve the shipped renderer when validating old bytes."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-01-shipped-render.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {lesson}\n\n**Work completed**\n- {status}",
                synthetic_rule=False,
            )
        ],
    )

    manifest = _legacy_v2_manifest(compile_memory, daily)

    assert manifest["source_blocks"] == [
        "\n".join(
            (
                "## [12:00:00] opencode-idle | session",
                f"- Project root JSON: {json.dumps(str(root.resolve()))}",
                f"- Project slug: `{_SYNTHETIC_PROJECT_SLUG}`",
                "",
                "**Lessons / patterns**",
                f"- {lesson}",
                "",
                "**Work completed**",
                f"- {status}",
            )
        )
    ]


@pytest.mark.parametrize("artifact_location", ("active", "retired"))
def test_legacy_pending_generation_is_retired_and_freshly_prepared(
    compile_env,
    artifact_location,
):
    compile_memory, root, state_root, state = compile_env
    rejected = "Status: implementation complete; twelve files changed."
    admitted = "Always revalidate persisted input before applying pending work."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-11-01-legacy-{artifact_location}.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {rejected}\n- {admitted}",
            )
        ],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _legacy_accepted_operation(
        daily,
        f"legacy-{artifact_location}",
        rejected,
        root,
    )
    _write_legacy_v1_journal(compile_memory, old_request, [operation])
    if artifact_location == "retired":
        _retire_legacy_generation(compile_memory, legacy, old_request)

    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    replacement = compile_memory._load_manifest(request["generation_id"])

    assert request["pending"] is True
    assert request["generation_id"] != legacy["generation_id"]
    assert replacement["version"] == 3
    assert admitted in "\n".join(request["source_blocks"])
    assert not (root / "knowledge" / "notes" / f"legacy-{artifact_location}.md").exists()
    assert not (
        state_root
        / "run"
        / "compile-journal"
        / f"{old_request['batch_id']}.json"
    ).exists()
    assert not (
        state_root
        / "run"
        / "compile-manifests"
        / f"{legacy['generation_id']}.json"
    ).exists()
    assert len(
        list(
            (state_root / "run" / "retired-journals").glob(
                f"{old_request['batch_id']}.*.json"
            )
        )
    ) == 1
    assert len(
        list(
            (state_root / "run" / "retired-manifests").glob(
                f"{legacy['generation_id']}.*.json"
            )
        )
    ) == 1


def test_legacy_applied_effect_is_bound_into_replacement_manifest(compile_env):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    applied_quote = "Always preserve a verified durable effect during schema migration."
    pending_quote = "Validate remaining evidence under the current admission rules."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-01-legacy-applied.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {applied_quote}",
            ),
            _project_block(
                "12:01:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {pending_quote}",
            ),
        ],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _legacy_accepted_operation(
        daily,
        "legacy-applied-effect",
        applied_quote,
        root,
    )
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "legacy-applied-effect.md"
    target.write_text(
        compile_memory._render_operation_result(operation, "", marker, fingerprint),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    effect = {
        "version": 2,
        "target": target.name,
        "before": None,
        "after": snapshot[1],
        "retained_artifact": None,
    }
    _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        states=["applied"],
        effects=[effect],
        status="complete",
    )
    before = target.read_bytes()

    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    replacement = compile_memory._load_manifest(request["generation_id"])
    reconciliation = replacement["legacy_reconciliation"]

    assert target.read_bytes() == before
    assert reconciliation["journal_ids"] == [old_request["batch_id"]]
    assert reconciliation["effects"][0]["marker"] == marker
    assert reconciliation["effects"][0]["fingerprint"] == fingerprint
    full_bullet_token = compile_memory._evidence_token(
        {
            "daily_date": daily.stem,
            "timestamp": "12:00:00",
            "quoted_text": f"Rule: {applied_quote}",
        }
    )
    substring_token = compile_memory._evidence_token(operation["evidence"][0])
    wildcard_token = compile_memory._evidence_wildcard_token(
        operation["evidence"][0]
    )
    assert reconciliation["consumed_evidence"] == [wildcard_token]
    assert state["compile_consumed_evidence"] == [wildcard_token]
    assert full_bullet_token not in state["compile_consumed_evidence"]
    assert substring_token not in state["compile_consumed_evidence"]

    reused = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "legacy-reused-effect",
                applied_quote,
                summary="A migrated legacy substring cannot release its full bullet.",
            )
        ),
        False,
    )
    assert reused["ok"] is False
    assert reused["status"] == "plan_rejected"
    assert "evidence was already consumed" in reused["error"]

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "legacy-fresh-effect",
                pending_quote,
                summary="Fresh evidence remains after legacy reconciliation.",
                timestamp="12:01:00",
            )
        ),
        False,
    )
    assert result["ok"] is True, result
    receipt = state["compiled_daily_receipts"][daily.name]

    assert receipt["journal_ids"] == [old_request["batch_id"], request["batch_id"]]
    assert {effect["target"] for effect in receipt["effects"]} == {
        "legacy-applied-effect.md",
        "legacy-fresh-effect.md",
    }
    assert memory_state.is_compile_receipt_valid(
        receipt,
        daily.name,
        replacement["daily"]["sha256"],
        root=root,
    )
    assert daily.name not in state.get("compile_legacy_reconciliations", {})


def test_shipped_unscoped_v1_effect_migrates_without_fabricated_project_fields(
    compile_env,
):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    applied_quote = "Always retain authentic unscoped v1 effects byte for byte."
    pending_quote = "Validate a distinct current citation after migration."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-01-shipped-v1-effect.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                    f"**Lessons / patterns**\n- {applied_quote}",
                synthetic_rule=False,
                ),
                _project_block(
                    "12:01:00",
                    root,
                    _SYNTHETIC_PROJECT_SLUG,
                    f"**Lessons / patterns**\n- {pending_quote}",
                    synthetic_rule=False,
                ),
        ],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _shipped_v1_operation(
        daily,
        "shipped-unscoped-v1-effect",
        applied_quote,
    )
    assert "project_slug" not in operation
    assert "project_root" not in operation
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "shipped-unscoped-v1-effect.md"
    target.write_text(
        _render_shipped_v1_create(
            compile_memory,
            operation,
            marker,
            fingerprint,
        ),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        states=["applied"],
        effects=[
            {
                "version": 2,
                "target": target.name,
                "before": None,
                "after": snapshot[1],
                "retained_artifact": None,
            }
        ],
        status="complete",
    )
    before = target.read_bytes()

    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    replacement = compile_memory._load_manifest(request["generation_id"])
    [effect] = replacement["legacy_reconciliation"]["effects"]

    assert target.read_bytes() == before
    assert "project_slug" not in effect
    assert "project_root" not in effect

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "shipped-v1-current-effect",
                pending_quote,
                summary="Current scoped evidence remains distinct after v1 migration.",
                    timestamp="12:01:00",
            )
        ),
        False,
    )
    assert result["ok"] is True, result
    receipt = state["compiled_daily_receipts"][daily.name]
    legacy_effect = next(
        item for item in receipt["effects"] if item["journal_id"] == old_request["batch_id"]
    )
    assert "project_slug" not in legacy_effect
    assert "project_root" not in legacy_effect
    assert memory_state.is_compile_receipt_valid(
        receipt,
        daily.name,
        replacement["daily"]["sha256"],
        root=root,
    )


def test_pre_effect_v1_complete_create_is_bound_during_migration(compile_env):
    compile_memory, root, _state_root, state = compile_env
    applied_quote = "Always preserve authenticated creates from pre-effect journals."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-01-pre-effect-v1.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {applied_quote}",
                synthetic_rule=False,
            )
        ],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _shipped_v1_operation(
        daily,
        "pre-effect-v1-create",
        applied_quote,
    )
    rendered_at = operation.pop("_rendered_at")
    rendered_date = operation.pop("_rendered_date")
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "pre-effect-v1-create.md"
    target.write_text(
        _render_shipped_v1_create(
            compile_memory,
            {
                **operation,
                "_rendered_at": rendered_at,
                "_rendered_date": rendered_date,
            },
            marker,
            fingerprint,
        ),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    journal = _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        states=["applied"],
        status="complete",
    )
    journal.pop("operation_recovery")
    journal.pop("operation_effects")
    compile_memory._write_journal(journal)
    before = target.read_bytes()

    request = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=30_000,
    )
    replacement = compile_memory._load_manifest(request["generation_id"])
    [effect] = replacement["legacy_reconciliation"]["effects"]

    assert target.read_bytes() == before
    assert effect["journal_id"] == old_request["batch_id"]
    assert effect["target"] == target.name
    assert effect["after"] == snapshot[1]
    assert effect["marker"] == marker
    assert effect["fingerprint"] == fingerprint


def test_golden_unscoped_v1_update_reconciles_scoped_target_without_reapply(
    compile_env,
    monkeypatch,
):
    import memory_state

    compile_memory, root, state_root, state = compile_env
    installed = _install_golden_v1_scoped_update(compile_memory, root, state)
    target_before_migration = installed["target"].read_bytes()

    def reject_new_operation(*_args, **_kwargs):
        pytest.fail("legacy migration must not execute a new operation")

    monkeypatch.setattr(compile_memory, "_execute_plan", reject_new_operation)

    result = compile_memory.prepare_compile_request(
        [installed["daily"]],
        state,
        prompt_char_budget=30_000,
    )

    assert result == {"pending": False}
    assert installed["target"].read_bytes() == target_before_migration
    receipt = state["compiled_daily_receipts"][installed["daily"].name]
    [effect] = receipt["effects"]
    assert effect["target"] == installed["target"].name
    assert effect["after"] == installed["after"]
    assert effect["marker"] == installed["marker"]
    assert effect["fingerprint"] == installed["fingerprint"]
    assert "project_slug" not in effect
    assert "project_root" not in effect
    assert effect["source_scope"] == "unscoped"
    [evidence] = installed["journal"]["accepted"]["operations"][0]["evidence"]
    wildcard = compile_memory._evidence_wildcard_token(evidence)
    assert receipt["consumed_evidence"] == [wildcard]
    assert state["compile_consumed_evidence"] == [wildcard]
    assert memory_state.is_compile_receipt_valid(
        receipt,
        installed["daily"].name,
        installed["manifest"]["daily"]["sha256"],
        root=root,
    )
    assert list(
        (state_root / "run" / "retired-manifests").glob(
            f"{installed['manifest']['generation_id']}.*.json"
        )
    )
    assert list(
        (state_root / "run" / "retired-journals").glob(
            f"{installed['journal']['batch_id']}.*.json"
        )
    )


@pytest.mark.parametrize(
    "malformed",
    (
        "effect-extra",
        "version",
        "target",
        "before-extra",
        "after-extra",
        "after-missing",
        "identity-shape",
        "identity-bool",
        "sha256",
        "size-bool",
        "mode-bool",
        "file-attributes-bool",
        "nlink-zero",
        "retained-artifact",
    ),
)
def test_malformed_legacy_effect_snapshot_makes_no_migration_progress(
    compile_env,
    malformed,
):
    compile_memory, root, state_root, state = compile_env
    installed = _install_golden_v1_scoped_update(
        compile_memory,
        root,
        state,
        malformed=malformed,
    )
    manifest_path = compile_memory._manifest_path(
        installed["manifest"]["generation_id"]
    )
    journal_path = compile_memory._journal_path(installed["journal"]["batch_id"])
    manifest_before = manifest_path.read_bytes()
    journal_before = journal_path.read_bytes()
    target_before = installed["target"].read_bytes()
    state_before = json.loads(json.dumps(state))

    with pytest.raises(
        (compile_memory.CompileManifestError, compile_memory.CompilePreparationError, ValueError)
    ):
        compile_memory.prepare_compile_request(
            [installed["daily"]],
            state,
            prompt_char_budget=30_000,
        )

    assert manifest_path.read_bytes() == manifest_before
    assert journal_path.read_bytes() == journal_before
    assert installed["target"].read_bytes() == target_before
    assert state == state_before
    assert not (state_root / "run" / "retired-manifests").exists()
    assert not (state_root / "run" / "retired-journals").exists()


@pytest.mark.parametrize("retired", (False, True), ids=("active", "retired"))
@pytest.mark.parametrize(
    ("case", "body", "legacy_quote", "blocked_bullet", "occurrence_limit"),
    (
        (
            "zero",
            "**Lessons / patterns**\n"
            "- Always preserve another current rule when migration is uncertain.\n\n"
            "**Work completed**\n"
            "- Legacy migration status changed during the shipped run.",
            "migration status changed",
            "Always preserve another current rule when migration is uncertain.",
            None,
        ),
        (
            "ambiguous",
            "**Lessons / patterns**\n"
            "- When replaying a shared marker, preserve the first durable rule.\n"
            "- When retiring a shared marker, preserve the second durable rule.",
            "shared marker",
            "When replaying a shared marker, preserve the first durable rule.",
            1,
        ),
    ),
)
def test_legacy_unresolved_substring_consumes_timestamp_wildcard(
    compile_env,
    monkeypatch,
    retired,
    case,
    body,
    legacy_quote,
    blocked_bullet,
    occurrence_limit,
):
    compile_memory, root, _state_root, state = compile_env
    location = "retired" if retired else "active"
    fresh_bullet = "When migration completes, preserve evidence from a later timestamp."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-11-03-{case}-{location}.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                body,
                synthetic_rule=False,
            ),
            _project_block(
                "12:01:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {fresh_bullet}",
                synthetic_rule=False,
            ),
        ],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _legacy_accepted_operation(
        daily,
        f"legacy-wildcard-{case}-{location}",
        legacy_quote,
        root,
    )
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / f"{operation['slug']}.md"
    target.write_text(
        compile_memory._render_operation_result(operation, "", marker, fingerprint),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        states=["applied"],
        effects=[
            {
                "version": 2,
                "target": target.name,
                "before": None,
                "after": snapshot[1],
                "retained_artifact": None,
            }
        ],
        status="complete",
    )
    if retired:
        _retire_legacy_generation(compile_memory, legacy, old_request)

    if occurrence_limit is None:
        request = compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )
    else:
        with monkeypatch.context() as bounded:
            bounded.setattr(
                compile_memory,
                "MAX_EVIDENCE_QUOTE_OCCURRENCES",
                occurrence_limit,
            )
            request = compile_memory.prepare_compile_request(
                [daily], state, prompt_char_budget=30_000
            )
    wildcard = compile_memory._canonical_digest(
        {
            "kind": "source_timestamp_wildcard",
            "daily_date": daily.stem,
            "timestamp": "12:00:00",
        }
    )
    substring_token = compile_memory._evidence_token(operation["evidence"][0])

    assert state["compile_consumed_evidence"] == [wildcard]
    assert substring_token not in state["compile_consumed_evidence"]
    assert compile_memory.load_state()["compile_consumed_evidence"] == [wildcard]

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                f"wildcard-reuse-{case}-{location}",
                blocked_bullet,
                summary="A timestamp wildcard must block every covered bullet.",
            )
        ),
        False,
    )

    assert result["ok"] is False
    assert result["status"] == "plan_rejected"
    assert "evidence was already consumed" in result["error"]

    allowed = _admission_operation(
        daily,
        f"wildcard-allowed-{case}-{location}",
        fresh_bullet,
        summary=f"Fresh allowed summary for {case} {location}.",
        timestamp="12:01:00",
    )
    applied = compile_memory.apply_compile_batch(
        request,
        _response(allowed),
        False,
    )
    assert applied["ok"] is True, applied
    receipt = state["compiled_daily_receipts"][daily.name]
    assert wildcard in receipt["consumed_evidence"]
    assert wildcard in state["compile_consumed_evidence"]

    allowed_target = root / "knowledge" / "notes" / f"{allowed['slug']}.md"
    allowed_target.unlink()
    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    boundary = state["compile_daily_replay_boundaries"][daily.name]
    assert wildcard in boundary["consumed_evidence"]
    replayed = compile_memory.apply_compile_batch(retry, _response(), False)

    assert replayed["ok"] is True, replayed
    assert allowed_target.is_file()
    assert wildcard in state["compile_consumed_evidence"]
    assert wildcard in state["compiled_daily_receipts"][daily.name][
        "consumed_evidence"
    ]


def test_legacy_migration_capacity_failure_keeps_active_artifacts(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Preserve this complete bullet when a legacy substring was applied."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-04-capacity.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _legacy_accepted_operation(
        daily,
        "legacy-capacity",
        quote,
        root,
    )
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "legacy-capacity.md"
    target.write_text(
        compile_memory._render_operation_result(operation, "", marker, fingerprint),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        states=["applied"],
        effects=[
            {
                "version": 2,
                "target": target.name,
                "before": None,
                "after": snapshot[1],
                "retained_artifact": None,
            }
        ],
        status="complete",
    )
    existing = "a" * 64
    state["compile_consumed_evidence"] = [existing]
    monkeypatch.setattr(compile_memory, "MAX_CONSUMED_EVIDENCE_TOKENS", 1)

    with pytest.raises(ValueError, match="consumed evidence capacity"):
        compile_memory.prepare_compile_request(
            [daily], state, prompt_char_budget=30_000
        )

    assert state["compile_consumed_evidence"] == [existing]
    assert daily.name not in state.get("compile_legacy_reconciliations", {})
    assert (
        state_root
        / "run"
        / "compile-manifests"
        / f"{legacy['generation_id']}.json"
    ).is_file()
    assert (
        state_root
        / "run"
        / "compile-journal"
        / f"{old_request['batch_id']}.json"
    ).is_file()


def test_same_plan_cannot_consume_one_evidence_token_twice(compile_env):
    compile_memory, root, state_root, state = compile_env
    quote = "One durable bullet can authorize at most one accepted operation."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-02-same-plan-token.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "same-token-first",
                quote,
                summary="The first proposed use of one durable token.",
            ),
            _admission_operation(
                daily,
                "same-token-second",
                quote,
                summary="The second proposed use of one durable token.",
            ),
        ),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "evidence was already consumed" in result["error"]
    assert not list((root / "knowledge" / "notes").glob("same-token-*.md"))


def test_retired_earlier_batch_reserves_evidence_for_later_batch(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_COMPLETED_JOURNALS", 0)
    quote = "Always reserve exact evidence from earlier accepted batches."
    blocks = [
        _project_block(
            "12:00:00",
            root,
            _SYNTHETIC_PROJECT_SLUG,
            (
                "**Lessons / patterns**\n"
                f"- {quote}\n\n"
                "**Open questions**\n"
                f"- Can {'a' * 14_000}{index} be retained?"
            ),
            synthetic_rule=False,
        )
        for index in range(2)
    ]
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-02-later-batch-token.md",
        blocks,
    )
    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    assert first["batch_count"] == 2
    first_result = compile_memory.apply_compile_batch(
        first,
        _response(
            _admission_operation(
                daily,
                "first-batch-token",
                quote,
                summary="The first batch reserves its admitted evidence.",
            )
        ),
        False,
    )
    assert first_result["ok"] is True
    assert first_result["daily_complete"] is False
    with compile_memory._bound_journal_directory(create=False) as bound:
        name = f"{first['batch_id']}.json"
        admitted = compile_memory._journal_file_metadata(bound, name)
        compile_memory._retire_journal_file(bound, name, admitted)
    assert not (
        state_root / "run" / "compile-journal" / f"{first['batch_id']}.json"
    ).exists()
    assert list(
        (state_root / "run" / "retired-journals").glob(
            f"{first['batch_id']}.*.json"
        )
    )

    second = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=24_000
    )
    result = compile_memory.apply_compile_batch(
        second,
        _response(
            _admission_operation(
                daily,
                "later-batch-token",
                quote,
                summary="A later batch must not reuse earlier evidence.",
            )
        ),
        False,
    )

    _assert_rejected_before_journal(result, second, daily, state_root, state)
    assert "evidence was already consumed" in result["error"]


def test_published_evidence_survives_restart_and_rejects_fresh_generation_reuse(
    compile_env,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Published evidence remains consumed across process restarts."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-02-restart-token.md",
        [_block("12:00:00", quote)],
    )
    first = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    first_operation = _admission_operation(daily, "restart-token-first", quote)
    assert compile_memory.apply_compile_batch(
        first,
        _response(first_operation),
        False,
    )["ok"]
    token = compile_memory._evidence_token(first_operation["evidence"][0])
    receipt = state["compiled_daily_receipts"][daily.name]
    assert receipt["consumed_evidence"] == [token]
    assert state["compile_consumed_evidence"] == [token]

    def force_fresh_generation(current: dict) -> None:
        for key in (
            "compiled_daily_hashes",
            "compiled_daily_receipts",
            "compile_daily_checkpoints",
            "compile_generation_active",
            "compile_sdk_progress",
            "compile_index_pending",
        ):
            current.pop(key, None)
        current["compile_generation_attempt_ids"] = {daily.name: "f" * 32}

    compile_memory.update_state(force_fresh_generation)
    restarted_state = compile_memory.load_state()
    request = compile_memory.prepare_compile_request(
        [daily], restarted_state, prompt_char_budget=30_000
    )
    manifest = compile_memory._load_manifest(request["generation_id"])
    assert manifest["consumed_evidence"] == [token]

    result = compile_memory.apply_compile_batch(
        request,
        _response(
            _admission_operation(
                daily,
                "restart-token-reuse",
                quote,
                summary="A fresh generation cannot reuse a published token.",
            )
        ),
        False,
    )
    restarted_state.update(compile_memory.load_state())

    _assert_rejected_before_journal(
        result,
        request,
        daily,
        state_root,
        restarted_state,
    )
    assert "evidence was already consumed" in result["error"]


def test_invalid_receipt_boundary_keeps_token_while_exact_journal_replays(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Receipt invalidation never releases accepted evidence for reuse."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-02-invalid-receipt-token.md",
        [_block("12:00:00", quote)],
    )
    slug = "invalid-receipt-token"
    target = root / "knowledge" / "notes" / f"{slug}.md"
    original = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    assert compile_memory.apply_compile_batch(
        original,
        _response(_admission_operation(daily, slug, quote)),
        False,
    )["ok"]
    token = state["compile_consumed_evidence"][0]
    target.unlink()

    def reject_substring_scan(*_args, **_kwargs):
        pytest.fail("journal replay must use exact indexed evidence")

    monkeypatch.setattr(
        compile_memory,
        "_substring_positions",
        reject_substring_scan,
        raising=False,
    )

    retry = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )
    boundary = state["compile_daily_replay_boundaries"][daily.name]
    assert boundary["consumed_evidence"] == [token]
    assert state["compile_consumed_evidence"] == [token]

    replayed = compile_memory.apply_compile_batch(retry, _response(), False)

    assert replayed["ok"] is True, replayed
    assert target.is_file()
    assert state["compile_consumed_evidence"] == [token]
    assert state["compiled_daily_receipts"][daily.name]["consumed_evidence"] == [token]
    assert (
        state_root / "run" / "compile-journal" / f"{original['batch_id']}.json"
    ).is_file()


def test_consumed_evidence_capacity_fails_closed_before_journal(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    monkeypatch.setattr(compile_memory, "MAX_CONSUMED_EVIDENCE_TOKENS", 1)
    existing = "a" * 64
    compile_memory.update_state(
        lambda current: current.update({"compile_consumed_evidence": [existing]})
    )
    state.update(compile_memory.load_state())
    quote = "Evidence capacity exhaustion must fail before durable admission."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-02-token-capacity.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(daily, "token-capacity", quote)),
        False,
    )

    _assert_rejected_before_journal(result, request, daily, state_root, state)
    assert "consumed evidence capacity" in result["error"]
    assert state["compile_consumed_evidence"] == [existing]


def test_legacy_recovery_required_is_resolved_without_applying_operation(compile_env):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    quote = "Always restore or discard prepared legacy updates before migration."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-01-legacy-recovery.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {quote}",
            )
        ],
    )
    target = root / "knowledge" / "notes" / "legacy-recovery.md"
    _write_note(target, "Legacy Recovery", "The original bytes remain authoritative.")
    target_snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert target_snapshot is not None
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _legacy_accepted_operation(
        daily,
        target.stem,
        quote,
        root,
        action="update",
        expected_target=target_snapshot[1],
    )
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    replacement = compile_memory._render_operation_result(
        operation,
        target_snapshot[0],
        marker,
        fingerprint,
    )
    with memory_state.bind_atomic_writes_to_directory(target.parent):
        recovery = memory_state.prepare_conditional_atomic_write(
            target,
            replacement,
            target_snapshot[1],
            fingerprint,
        )
    _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        recoveries=[recovery],
        status="recovery_required",
    )
    before = target.read_bytes()

    request = compile_memory.prepare_compile_request(
        [daily], state, prompt_char_budget=30_000
    )

    assert request["pending"] is True
    assert request["generation_id"] != legacy["generation_id"]
    assert target.read_bytes() == before
    assert marker.encode("ascii") not in before


def test_legacy_readers_reject_rehashed_version_specific_field_drift(compile_env):
    compile_memory, root, _state_root, state = compile_env
    quote = "Always validate exact legacy fields after checking their hashes."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-01-legacy-fields.md",
        [_block("12:00:00", quote)],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _legacy_accepted_operation(daily, "legacy-fields", quote, root)
    journal = _write_legacy_v1_journal(
        compile_memory,
        request,
        [operation],
    )

    manifest_path = compile_memory._manifest_path(legacy["generation_id"])
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["unexpected"] = True
    manifest_data["manifest_sha256"] = compile_memory._manifest_digest(manifest_data)
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    journal_path = compile_memory._journal_path(request["batch_id"])
    journal["unexpected"] = True
    journal["journal_sha256"] = compile_memory._journal_digest(journal)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(compile_memory.CompileManifestError, match="fields are invalid"):
        compile_memory._load_manifest(legacy["generation_id"])
    with pytest.raises(ValueError, match="fields are invalid"):
        compile_memory._load_journal(request["batch_id"])


def _apply_nested_journal_drift(journal: dict, drift: str) -> None:
    operation = journal["accepted"]["operations"][0]
    if drift == "operation-extra":
        operation["unexpected"] = True
    elif drift == "operation-type":
        operation["title"] = False
    elif drift == "evidence-extra":
        operation["evidence"][0]["unexpected"] = True
    elif drift == "evidence-type":
        operation["evidence"][0]["claim"] = False
    elif drift == "audit-extra":
        journal["accepted"]["audit"]["unexpected"] = True
    elif drift == "audit-type":
        journal["accepted"]["audit"]["verified"] = False
    elif drift == "source-extra":
        journal["accepted"]["source"][0]["unexpected"] = True
    elif drift == "source-type":
        journal["accepted"]["source"][0]["path"] = False
    elif drift in {"recovery-extra", "recovery-type"}:
        journal["operation_states"] = ["pending"]
        journal["operation_effects"] = [None]
        journal["operation_recovery"] = [
            {
                "version": 1,
                "kind": "unresolved",
                "status": "required",
                "owned_paths": [],
            }
        ]
        journal["status"] = "recovery_required"
        if drift == "recovery-extra":
            journal["operation_recovery"][0]["unexpected"] = True
        else:
            journal["operation_recovery"][0]["owned_paths"] = False
    elif drift == "effect-extra":
        journal["operation_effects"][0]["unexpected"] = True
    elif drift == "effect-type":
        journal["operation_effects"][0]["target"] = False
    else:
        raise AssertionError(f"unknown journal drift case: {drift}")


@pytest.mark.parametrize("journal_version", (1, 2), ids=("shipped-v1", "current-v2"))
def test_rehashed_journal_rejects_nested_key_and_type_drift(
    compile_env,
    journal_version,
):
    compile_memory, root, _state_root, state = compile_env
    if journal_version == 1:
        baseline = _install_golden_v1_scoped_update(
            compile_memory,
            root,
            state,
        )["journal"]
    else:
        quote = "Always reject rehashed nested journal schema drift before recovery."
        daily = _daily(
            root / "knowledge" / "daily" / "2026-11-01-journal-nested.md",
            [
                _project_block(
                    "12:00:00",
                    root,
                    _SYNTHETIC_PROJECT_SLUG,
                    f"**Lessons / patterns**\n- {quote}",
                )
            ],
        )
        request = compile_memory.prepare_compile_request(
            [daily],
            state,
            prompt_char_budget=30_000,
        )
        result = compile_memory.apply_compile_batch(
            request,
            _response(_admission_operation(daily, "journal-nested", quote)),
            False,
        )
        assert result["ok"] is True
        baseline = compile_memory._load_journal(request["batch_id"])
        assert baseline is not None

    drift_cases = (
        "operation-extra",
        "operation-type",
        "evidence-extra",
        "evidence-type",
        "audit-extra",
        "audit-type",
        "source-extra",
        "source-type",
        "recovery-extra",
        "recovery-type",
        "effect-extra",
        "effect-type",
    )
    admitted = []
    for drift in drift_cases:
        journal = json.loads(json.dumps(baseline))
        _apply_nested_journal_drift(journal, drift)
        journal["accepted_sha256"] = compile_memory._canonical_digest(
            journal["accepted"]
        )
        journal["journal_sha256"] = compile_memory._journal_digest(journal)
        compile_memory._journal_path(journal["batch_id"]).write_text(
            json.dumps(journal, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            compile_memory._load_journal(journal["batch_id"])
        except ValueError:
            continue
        admitted.append(drift)

    assert admitted == [], f"rehashed nested drift was admitted: {admitted}"


def test_shipped_v1_journal_accepts_rollback_with_unavailable_restore_snapshot(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    installed = _install_golden_v1_scoped_update(
        compile_memory,
        root,
        state,
    )
    journal = installed["journal"]
    target = installed["target"]
    token = "b" * 32
    journal["operation_states"] = ["pending"]
    journal["operation_recovery"] = [
        {
            "version": 1,
            "kind": "rollback",
            "status": "required",
            "target": target.name,
            "token": token,
            "displaced_path": f".{target.name}.{token}.displaced",
            "rollback_backup_path": f".{target.name}.{token}.rejected",
            "attempted": installed["after"],
            "restore": None,
            "attempted_artifact_path": None,
            "owned_paths": [],
        }
    ]
    journal["operation_effects"] = [None]
    journal["status"] = "recovery_required"
    compile_memory._write_journal(journal)

    assert compile_memory._load_journal(journal["batch_id"]) == journal


@pytest.mark.parametrize("entrypoint", ("sdk-prepare", "direct"))
def test_production_resume_entrypoints_migrate_legacy_index_pending_before_current_match(
    compile_env,
    monkeypatch,
    entrypoint,
):
    compile_memory, root, _state_root, state = compile_env
    installed = _install_golden_v1_scoped_update(compile_memory, root, state)
    journal = installed["journal"]
    manifest = installed["manifest"]
    journal["status"] = "index_pending"
    compile_memory._write_journal(journal)
    state["compile_index_pending"] = {
        "batch_id": journal["batch_id"],
        "daily": installed["daily"].name,
        "sha256": manifest["daily"]["sha256"],
        "generation_id": manifest["generation_id"],
    }
    current_match_calls = []
    observed_states = []
    monkeypatch.setattr(
        compile_memory,
        "_journal_matches_manifest",
        lambda *_args, **_kwargs: current_match_calls.append(1) or False,
    )

    if entrypoint == "sdk-prepare":
        monkeypatch.setattr(
            compile_memory,
            "select_dailies",
            lambda _args, _state: [],
        )

        def prepare_after_resume(_paths, current, *, prompt_char_budget):
            del prompt_char_budget
            observed_states.append(json.loads(json.dumps(current)))
            return {"pending": False}

        monkeypatch.setattr(
            compile_memory,
            "prepare_compile_request",
            prepare_after_resume,
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["compile_memory.py", "--prepare-sdk-request"],
        )
    else:
        def run_after_resume(_args):
            observed_states.append(compile_memory.load_state())
            return 0

        monkeypatch.setattr(compile_memory, "_run", run_after_resume)
        monkeypatch.setattr(sys, "argv", ["compile_memory.py"])

    assert compile_memory.main() == 0

    assert current_match_calls == []
    assert observed_states
    assert "compile_index_pending" not in observed_states[0]
    reconciliation = observed_states[0]["compile_legacy_reconciliations"][
        installed["daily"].name
    ]
    assert reconciliation["journal_ids"] == [journal["batch_id"]]
    assert reconciliation["effects"][0]["marker"] == installed["marker"]


@pytest.mark.parametrize("effect_recorded", (False, True), ids=("before-effect", "after-effect"))
def test_legacy_migration_repairs_marker_complete_create_write_windows(
    compile_env,
    effect_recorded,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Always retain a marker-complete legacy create across migration."
    daily = _daily(
        root / "knowledge" / "daily" / f"2026-11-05-create-{effect_recorded}.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {quote}",
                synthetic_rule=False,
            )
        ],
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _shipped_v1_operation(daily, "legacy-create-window", quote)
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "legacy-create-window.md"
    target.write_text(
        _render_shipped_v1_create(
            compile_memory,
            operation,
            marker,
            fingerprint,
        ),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    effect = {
        "version": 2,
        "target": target.name,
        "before": None,
        "after": snapshot[1],
        "retained_artifact": None,
    }
    _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        states=["pending"],
        effects=[effect if effect_recorded else None],
        status="applying",
    )
    target_before = target.read_bytes()

    request = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=30_000,
    )
    replacement = compile_memory._load_manifest(request["generation_id"])
    [reconciled] = replacement["legacy_reconciliation"]["effects"]
    retired = compile_memory._load_journal(old_request["batch_id"])

    assert target.read_bytes() == target_before
    assert target.read_text(encoding="utf-8").count(marker) == 1
    assert reconciled["target"] == target.name
    assert reconciled["after"] == snapshot[1]
    assert retired["operation_states"] == ["applied"]
    assert retired["operation_effects"] == [effect]
    assert state["compile_consumed_evidence"] == [
        compile_memory._evidence_wildcard_token(operation["evidence"][0])
    ]


def test_legacy_migration_repairs_cleanup_pending_update_after_effect_record(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Always finish a recorded legacy update effect before migration."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-05-update-effect.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {quote}",
                synthetic_rule=False,
            )
        ],
    )
    target = root / "knowledge" / "notes" / "legacy-update-window.md"
    _write_note(
        target,
        "Legacy Update Window",
        "The original target is durable before the update.",
        project_slug=_SYNTHETIC_PROJECT_SLUG,
        project_root=root,
    )
    before = compile_memory._read_knowledge_page_snapshot(target)
    assert before is not None
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    operation = _legacy_accepted_operation(
        daily,
        target.stem,
        quote,
        root,
        action="update",
        expected_target=before[1],
    )
    journal = _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        status="applying",
    )
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    replacement_content = compile_memory._render_operation_result(
        operation,
        before[0],
        marker,
        fingerprint,
    )
    with compile_memory.bind_atomic_writes_to_directory(target.parent):
        recovery = compile_memory.prepare_conditional_atomic_write(
            target,
            replacement_content,
            before[1],
            fingerprint,
        )
        journal["operation_recovery"][0] = recovery
        compile_memory._write_journal(journal)
        compile_memory.conditional_atomic_write(
            target,
            recovery,
            persist_recovery=lambda _recovery: compile_memory._write_journal(journal),
        )
        journal["operation_states"][0] = "cleanup_pending"
        recovery["status"] = "cleanup_pending"
        compile_memory._write_journal(journal)
        compile_memory.finalize_conditional_atomic_write(
            target,
            recovery,
            persist_recovery=lambda _recovery: compile_memory._write_journal(journal),
        )
        compile_memory._record_operation_effect(journal, 0)
    crash_effect = json.loads(json.dumps(journal["operation_effects"][0]))
    target_before = target.read_bytes()
    assert journal["operation_states"] == ["cleanup_pending"]

    request = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=30_000,
    )
    replacement = compile_memory._load_manifest(request["generation_id"])
    [reconciled] = replacement["legacy_reconciliation"]["effects"]
    retired = compile_memory._load_journal(old_request["batch_id"])

    assert target.read_bytes() == target_before
    assert target.read_text(encoding="utf-8").count(marker) == 1
    assert reconciled["after"] == crash_effect["after"]
    assert retired["operation_states"] == ["applied"]
    assert retired["operation_recovery"] == [None]
    assert retired["operation_effects"] == [crash_effect]


def test_v1_receipt_boundary_recovers_legacy_timestamp_provenance(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    installed = _install_golden_v1_scoped_update(compile_memory, root, state)
    journal = installed["journal"]
    target = installed["target"]
    receipt = {
        "version": 1,
        "daily_sha256": installed["manifest"]["daily"]["sha256"],
        "generation_id": installed["manifest"]["generation_id"],
        "journal_ids": [journal["batch_id"]],
        "effects": [
            {
                "journal_id": journal["batch_id"],
                "operation_index": 0,
                "target": target.name,
                "after": installed["after"],
                "marker": installed["marker"],
                "fingerprint": installed["fingerprint"],
            }
        ],
        "targets": [{"target": target.name, "current": installed["after"]}],
        "index": {
            "generation_id": installed["manifest"]["generation_id"],
            "entries": [f"knowledge/notes/{target.stem}"],
        },
    }

    boundary = compile_memory._receipt_replay_boundary(
        receipt,
        installed["manifest"]["daily"]["sha256"],
    )

    [evidence] = journal["accepted"]["operations"][0]["evidence"]
    assert boundary["consumed_evidence"] == [
        compile_memory._evidence_wildcard_token(evidence)
    ]

    compile_memory._journal_path(journal["batch_id"]).unlink()
    with pytest.raises(
        compile_memory.CompilePreparationError,
        match="v1 receipt evidence reconstruction failed",
    ):
        compile_memory._receipt_replay_boundary(
            receipt,
            installed["manifest"]["daily"]["sha256"],
        )


def test_v1_receipt_restart_preserves_exact_current_evidence_through_replay(
    compile_env,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "A v1 receipt restart must retain exact current evidence provenance."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-06-v1-receipt.md",
        [_block("12:00:00", quote)],
    )
    operation = _admission_operation(daily, "v1-receipt-restart", quote)
    original = compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=30_000,
    )
    assert compile_memory.apply_compile_batch(
        original,
        _response(operation),
        False,
    )["ok"]
    token = compile_memory._evidence_token(operation["evidence"][0])
    old_receipt = json.loads(json.dumps(state["compiled_daily_receipts"][daily.name]))
    old_receipt["version"] = 1
    old_receipt.pop("consumed_evidence")
    old_receipt.pop("generation_lineage")
    for effect in old_receipt["effects"]:
        effect.pop("project_slug", None)
        effect.pop("project_root", None)
    state["compiled_daily_receipts"][daily.name] = old_receipt
    state.pop("compile_consumed_evidence")
    target = root / "knowledge" / "notes" / "v1-receipt-restart.md"
    target.unlink()
    restarted = compile_memory.load_state()

    retry = compile_memory.prepare_compile_request(
        [daily],
        restarted,
        prompt_char_budget=30_000,
    )

    boundary = state["compile_daily_replay_boundaries"][daily.name]
    assert boundary["consumed_evidence"] == [token]
    replayed = compile_memory.apply_compile_batch(retry, _response(), False)
    assert replayed["ok"] is True, replayed
    assert target.is_file()
    assert state["compile_consumed_evidence"] == [token]
    assert state["compiled_daily_receipts"][daily.name]["consumed_evidence"] == [token]


def test_shipped_v2_index_pending_completion_binds_generation_lineage(
    compile_env,
):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    fixture_path = Path(__file__).parent / "fixtures" / "compile-v2-93be6b8.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["provenance"]["commit"] == "93be6b8"
    manifest = fixture["manifest"]
    journal = fixture["journal"]
    daily = root / manifest["daily"]["path"]
    daily.write_bytes(fixture["source_utf8"].encode("utf-8"))
    compile_memory._write_new_manifest(manifest)
    compile_memory._write_journal(journal)
    state["compile_generation_active"] = {
        daily.name: {
            "generation_id": manifest["generation_id"],
            "source_sha256": manifest["daily"]["sha256"],
        }
    }
    state["compile_index_pending"] = {
        "batch_id": journal["batch_id"],
        "daily": daily.name,
        "sha256": manifest["daily"]["sha256"],
        "generation_id": manifest["generation_id"],
    }

    resumed = compile_memory._resume_pending_index_if_any()

    assert resumed == {
        "ok": True,
        "status": "legacy_migrated",
        "daily_complete": False,
    }
    reconciliation = state["compile_legacy_reconciliations"][daily.name]
    assert reconciliation["generation_lineage"] == [manifest["generation_id"]]

    assert compile_memory.prepare_compile_request(
        [daily],
        state,
        prompt_char_budget=30_000,
    ) == {"pending": False}

    receipt = state["compiled_daily_receipts"][daily.name]
    replacement = compile_memory._load_manifest(receipt["generation_id"])
    assert replacement["version"] == 3
    assert replacement["generation_id"] != manifest["generation_id"]
    assert replacement["legacy_reconciliation"]["generation_lineage"] == [
        manifest["generation_id"]
    ]
    assert receipt["generation_lineage"] == [manifest["generation_id"]]
    assert memory_state.is_compile_receipt_valid(
        receipt,
        daily.name,
        manifest["daily"]["sha256"],
        root=root,
    )


def test_v3_generation_identity_binds_exact_predecessor_lineage(compile_env):
    compile_memory, root, _state_root, _state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-07-lineage-identity.md",
        [_block("12:00:00", "Generation lineage must remain part of immutable identity.")],
    )
    source_bytes, source_text = compile_memory._read_daily_snapshot(daily)
    context = compile_memory._compile_context_snapshot(30_000)
    no_lineage = compile_memory._empty_legacy_reconciliation()
    predecessor = hashlib.sha256(b"shipped-v2-generation").hexdigest()
    linked = {
        **no_lineage,
        "generation_lineage": [predecessor],
    }

    baseline = compile_memory._derive_manifest(
        daily,
        source_text,
        30_000,
        context,
        None,
        source_bytes=source_bytes,
        legacy_reconciliation=no_lineage,
    )
    migrated = compile_memory._derive_manifest(
        daily,
        source_text,
        30_000,
        context,
        None,
        source_bytes=source_bytes,
        legacy_reconciliation=linked,
    )

    assert no_lineage["generation_lineage"] == []
    assert compile_memory._validate_manifest_legacy_reconciliation(linked) == linked
    assert baseline["generation_id"] != migrated["generation_id"]


@pytest.mark.parametrize(
    "lineage",
    (
        "not-an-array",
        [{}],
        ["z" * 64],
        ["a" * 64, "a" * 64],
        [hashlib.sha256(f"lineage-{index}".encode()).hexdigest() for index in range(17)],
    ),
    ids=("wrong-type", "unhashable", "bad-digest", "duplicate", "over-capacity"),
)
def test_legacy_reconciliation_rejects_malformed_generation_lineage(
    compile_env,
    lineage,
):
    compile_memory, _root, _state_root, _state = compile_env
    reconciliation = {
        "version": 1,
        "generation_lineage": lineage,
        "predecessor_journal_ids": [],
        "journal_ids": [],
        "effects": [],
        "consumed_evidence": [],
    }

    with pytest.raises(ValueError):
        compile_memory._validate_manifest_legacy_reconciliation(reconciliation)


def _replay_lineage_records(compile_memory):
    predecessor = hashlib.sha256(b"predecessor-generation").hexdigest()
    current = hashlib.sha256(b"current-generation").hexdigest()
    predecessor_journal = hashlib.sha256(b"predecessor-journal").hexdigest()
    current_journal = hashlib.sha256(b"current-journal").hexdigest()
    daily_sha256 = hashlib.sha256(b"daily-source").hexdigest()
    boundary = {
        "version": 1,
        "daily_sha256": daily_sha256,
        "generation_id": predecessor,
        "journal_ids": [predecessor_journal],
        "effects": [],
        "consumed_evidence": [],
        "requires_nonempty": False,
    }
    manifest = {
        "generation_id": current,
        "batch_ids": [current_journal],
        "legacy_reconciliation": {
            "version": 1,
            "generation_lineage": [predecessor],
            "predecessor_journal_ids": [predecessor_journal],
            "journal_ids": [],
            "effects": [],
            "consumed_evidence": [],
        },
    }
    receipt = {
        "daily_sha256": daily_sha256,
        "generation_id": current,
        "generation_lineage": [predecessor],
        "journal_ids": [predecessor_journal, current_journal],
        "effects": [],
        "consumed_evidence": [],
    }
    return boundary, receipt, manifest, daily_sha256


def test_replay_boundary_accepts_cryptographically_linked_predecessor(
    compile_env,
):
    compile_memory, _root, _state_root, _state = compile_env
    boundary, receipt, manifest, daily_sha256 = _replay_lineage_records(
        compile_memory
    )

    assert compile_memory._replay_boundary_satisfied(
        boundary,
        receipt,
        daily_sha256,
        manifest,
    )


@pytest.mark.parametrize(
    "mutation",
    ("receipt-only", "manifest-only", "different-predecessor", "current-mismatch"),
)
def test_replay_boundary_rejects_unlinked_generation_claims(
    compile_env,
    mutation,
):
    compile_memory, _root, _state_root, _state = compile_env
    boundary, receipt, manifest, daily_sha256 = _replay_lineage_records(
        compile_memory
    )
    if mutation == "receipt-only":
        manifest["legacy_reconciliation"]["generation_lineage"] = []
    elif mutation == "manifest-only":
        receipt["generation_lineage"] = []
    elif mutation == "different-predecessor":
        different = hashlib.sha256(b"different-predecessor").hexdigest()
        manifest["legacy_reconciliation"]["generation_lineage"] = [different]
        receipt["generation_lineage"] = [different]
    else:
        receipt["generation_id"] = hashlib.sha256(b"different-current").hexdigest()

    assert not compile_memory._replay_boundary_satisfied(
        boundary,
        receipt,
        daily_sha256,
        manifest,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "manifest-omits-predecessor",
        "receipt-omits-predecessor",
        "receipt-has-extra",
        "boundary-has-extra",
    ),
)
def test_replay_boundary_rejects_inexact_predecessor_journal_union(
    compile_env,
    mutation,
):
    compile_memory, _root, _state_root, _state = compile_env
    boundary, receipt, manifest, daily_sha256 = _replay_lineage_records(
        compile_memory
    )
    if mutation == "manifest-omits-predecessor":
        manifest["legacy_reconciliation"]["predecessor_journal_ids"] = []
    elif mutation == "receipt-omits-predecessor":
        receipt["journal_ids"] = list(manifest["batch_ids"])
    elif mutation == "receipt-has-extra":
        receipt["journal_ids"].append(hashlib.sha256(b"extra-journal").hexdigest())
    else:
        boundary["generation_id"] = manifest["generation_id"]
        boundary["journal_ids"].append(hashlib.sha256(b"extra-journal").hexdigest())

    assert not compile_memory._replay_boundary_satisfied(
        boundary,
        receipt,
        daily_sha256,
        manifest,
    )


def _install_v1_receipt_reconstruction_case(compile_env):
    import memory_state

    compile_memory, root, state_root, state = compile_env
    quote = "A v1 receipt must reconstruct evidence before replay can begin."
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-08-v1-reconstruction.md",
        [
            _project_block(
                "12:00:00",
                root,
                _SYNTHETIC_PROJECT_SLUG,
                f"**Lessons / patterns**\n- {quote}",
                synthetic_rule=False,
            )
        ],
    )
    manifest = _legacy_v2_manifest(compile_memory, daily)
    request = _activate_legacy_manifest(compile_memory, state, daily, manifest)
    operation = _shipped_v1_operation(
        daily,
        "v1-reconstruction-target",
        quote,
    )
    marker = compile_memory._operation_marker(request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "v1-reconstruction-target.md"
    target.write_text(
        _render_shipped_v1_create(
            compile_memory,
            operation,
            marker,
            fingerprint,
        ),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    journal = _write_legacy_v1_journal(
        compile_memory,
        request,
        [operation],
        states=["applied"],
        effects=[
            {
                "version": 2,
                "target": target.name,
                "before": None,
                "after": snapshot[1],
                "retained_artifact": None,
            }
        ],
        status="complete",
    )
    sentinel = root / "knowledge" / "notes" / "v1-reconstruction-sentinel.md"
    _write_note(
        sentinel,
        "V1 Reconstruction Sentinel",
        "Preparation failures must leave this note byte-exact.",
    )
    _rebuild_test_index(compile_memory)
    receipt = {
        "version": 1,
        "daily_sha256": manifest["daily"]["sha256"],
        "generation_id": manifest["generation_id"],
        "journal_ids": [journal["batch_id"]],
        "effects": [
            {
                "journal_id": journal["batch_id"],
                "operation_index": 0,
                "target": target.name,
                "after": snapshot[1],
                "marker": marker,
                "fingerprint": fingerprint,
            }
        ],
        "targets": [{"target": target.name, "current": snapshot[1]}],
        "index": {
            "generation_id": manifest["generation_id"],
            "entries": [f"knowledge/notes/{target.stem}"],
        },
    }
    assert memory_state.is_compile_receipt_valid(
        receipt,
        daily.name,
        manifest["daily"]["sha256"],
        root=root,
    )
    state.pop("compile_generation_active")
    state["compiled_daily_hashes"] = {
        daily.name: manifest["daily"]["sha256"]
    }
    state["compiled_daily_receipts"] = {daily.name: receipt}
    return {
        "compile_memory": compile_memory,
        "root": root,
        "state_root": state_root,
        "state": state,
        "daily": daily,
        "target": target,
        "sentinel": sentinel,
        "journal": journal,
    }


@pytest.mark.parametrize(
    "failure",
    ("missing", "inconsistent", "unreadable"),
)
def test_v1_receipt_reconstruction_failure_precedes_every_mutation(
    compile_env,
    monkeypatch,
    failure,
):
    installed = _install_v1_receipt_reconstruction_case(compile_env)
    compile_memory = installed["compile_memory"]
    journal = installed["journal"]
    journal_path = compile_memory._journal_path(journal["batch_id"])
    if failure == "missing":
        journal_path.unlink()
    elif failure == "inconsistent":
        journal["accepted"]["operations"][0]["summary"] = (
            "A changed operation cannot authenticate the old receipt marker."
        )
        journal["accepted_sha256"] = compile_memory._canonical_digest(
            journal["accepted"]
        )
        compile_memory._write_journal(journal)
    else:
        real_load_journal = compile_memory._load_journal

        def unreadable_journal(journal_id, *args, **kwargs):
            if journal_id == journal["batch_id"]:
                raise OSError("injected v1 journal read failure")
            return real_load_journal(journal_id, *args, **kwargs)

        monkeypatch.setattr(compile_memory, "_load_journal", unreadable_journal)

    installed["target"].unlink()
    with compile_memory._global_compile_lock(
        timeout=compile_memory.COMPILE_LOCK_TIMEOUT_SECONDS
    ):
        pass
    state_before = json.loads(json.dumps(installed["state"]))
    runtime_before = {
        path.relative_to(installed["state_root"]).as_posix(): path.read_bytes()
        for path in installed["state_root"].rglob("*")
        if path.is_file()
    }
    sentinel_before = installed["sentinel"].read_bytes()
    index_before = compile_memory.INDEX.read_bytes()

    def reject_operation(*_args, **_kwargs):
        pytest.fail("v1 evidence failure must precede note operations")

    def reject_index():
        pytest.fail("v1 evidence failure must precede index rebuilding")

    monkeypatch.setattr(compile_memory, "_execute_plan", reject_operation)
    monkeypatch.setattr(compile_memory, "rebuild_index", reject_index)

    with pytest.raises(
        compile_memory.CompilePreparationError,
        match="v1 receipt evidence reconstruction failed",
    ):
        compile_memory.prepare_compile_request(
            [installed["daily"]],
            installed["state"],
            prompt_char_budget=30_000,
        )

    runtime_after = {
        path.relative_to(installed["state_root"]).as_posix(): path.read_bytes()
        for path in installed["state_root"].rglob("*")
        if path.is_file()
    }
    assert installed["state"] == state_before
    assert runtime_after == runtime_before
    assert installed["sentinel"].read_bytes() == sentinel_before
    assert compile_memory.INDEX.read_bytes() == index_before
    assert not installed["target"].exists()


def test_v1_receipt_migration_retains_effectful_and_effectless_journal_provenance(
    compile_env,
):
    import memory_state

    compile_memory, root, _state_root, state = compile_env
    effect_quote = (
        "Effectful predecessor journals must retain their exact effect. "
        + "effect provenance " * 100
    ).strip()
    empty_quote = (
        "Effectless predecessor journals must retain provenance without effects. "
        + "empty provenance " * 100
    ).strip()
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-09-mixed-v1-journals.md",
        [
            _block("12:00:00", effect_quote),
            _block("12:01:00", empty_quote),
        ],
    )
    source_bytes, source_text = compile_memory._read_daily_snapshot(daily)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    legacy_blocks = compile_memory._extract_legacy_manifest_blocks(source_text)
    context = compile_memory._compile_context_snapshot(30_000)
    single_batch_costs = []
    for block in legacy_blocks:
        request = compile_memory.build_compile_request(
            [daily],
            daily_blocks=[block],
            prompt_char_budget=1_000_000,
            context_snapshot=context,
            daily_sha256=source_sha256,
        )
        single_batch_costs.append(len(request["prompt"]) + len(request["system_prompt"]))
    budget = max(single_batch_costs) + 512
    manifest = compile_memory._derive_manifest_v2(
        daily,
        source_text,
        budget,
        context,
        None,
        source_bytes=source_bytes,
        source_sha256=source_sha256,
    )
    assert len(manifest["batch_ids"]) == 2
    compile_memory._write_new_manifest(manifest)
    effectful_id, effectless_id = manifest["batch_ids"]
    effectful_request = compile_memory._request_from_manifest(manifest, effectful_id)
    effectless_request = compile_memory._request_from_manifest(manifest, effectless_id)

    operation = _shipped_v1_operation(
        daily,
        "mixed-v1-journal-provenance",
        effect_quote,
    )
    marker = compile_memory._operation_marker(effectful_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "mixed-v1-journal-provenance.md"
    target.write_text(
        _render_shipped_v1_create(
            compile_memory,
            operation,
            marker,
            fingerprint,
        ),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    _write_legacy_v1_journal(
        compile_memory,
        effectful_request,
        [operation],
        states=["applied"],
        effects=[
            {
                "version": 2,
                "target": target.name,
                "before": None,
                "after": snapshot[1],
                "retained_artifact": None,
            }
        ],
        status="complete",
    )
    _write_legacy_v1_journal(
        compile_memory,
        effectless_request,
        [],
        states=[],
        effects=[],
        status="complete",
    )
    _rebuild_test_index(compile_memory)
    receipt = {
        "version": 1,
        "daily_sha256": source_sha256,
        "generation_id": manifest["generation_id"],
        "journal_ids": [effectful_id, effectless_id],
        "effects": [
            {
                "journal_id": effectful_id,
                "operation_index": 0,
                "target": target.name,
                "after": snapshot[1],
                "marker": marker,
                "fingerprint": fingerprint,
            }
        ],
        "targets": [{"target": target.name, "current": snapshot[1]}],
        "index": {
            "generation_id": manifest["generation_id"],
            "entries": [f"knowledge/notes/{target.stem}"],
        },
    }
    assert memory_state.is_compile_receipt_valid(
        receipt,
        daily.name,
        source_sha256,
        root=root,
    )
    state["compiled_daily_hashes"] = {daily.name: source_sha256}
    state["compiled_daily_receipts"] = {daily.name: receipt}
    compile_memory.INDEX.write_text("# Memory Index\n", encoding="utf-8")

    current_batch_ids = []
    for _ in range(10):
        request = compile_memory.prepare_compile_request(
            [daily],
            state,
            prompt_char_budget=budget,
        )
        assert request["pending"] is True
        current_batch_ids = request["batch_ids"]
        result = compile_memory.apply_compile_batch(request, _response(), False)
        assert result["ok"] is True, result
        if result["daily_complete"]:
            break
    else:
        pytest.fail("replacement v3 generation did not complete")

    final_receipt = state["compiled_daily_receipts"][daily.name]
    replacement = compile_memory._load_manifest(final_receipt["generation_id"])
    reconciliation = replacement["legacy_reconciliation"]
    assert reconciliation["predecessor_journal_ids"] == [
        effectful_id,
        effectless_id,
    ]
    assert reconciliation["journal_ids"] == [effectful_id]
    assert [effect["journal_id"] for effect in reconciliation["effects"]] == [
        effectful_id
    ]
    assert final_receipt["journal_ids"] == list(
        dict.fromkeys([effectful_id, effectless_id, *current_batch_ids])
    )
    assert [effect["journal_id"] for effect in final_receipt["effects"]] == [
        effectful_id
    ]
    assert memory_state.is_compile_receipt_valid(
        final_receipt,
        daily.name,
        source_sha256,
        root=root,
    )
    assert daily.name not in state.get("compile_daily_replay_boundaries", {})


def test_legacy_reconciliation_accepts_bounded_predecessor_journal_ids(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    predecessor_generation = hashlib.sha256(b"predecessor-generation").hexdigest()
    effectful_id = hashlib.sha256(b"effectful-journal").hexdigest()
    effectless_id = hashlib.sha256(b"effectless-journal").hexdigest()
    reconciliation = {
        "version": 1,
        "generation_lineage": [predecessor_generation],
        "predecessor_journal_ids": [effectful_id, effectless_id],
        "journal_ids": [],
        "effects": [],
        "consumed_evidence": [],
    }

    assert compile_memory._validate_manifest_legacy_reconciliation(
        reconciliation
    ) == reconciliation


@pytest.mark.parametrize(
    "predecessor_journal_ids",
    (
        "not-an-array",
        [{}],
        ["z" * 64],
        ["a" * 64, "a" * 64],
        [hashlib.sha256(f"journal-{index}".encode()).hexdigest() for index in range(1025)],
    ),
    ids=("wrong-type", "unhashable", "bad-digest", "duplicate", "over-capacity"),
)
def test_legacy_reconciliation_rejects_malformed_predecessor_journal_ids(
    compile_env,
    predecessor_journal_ids,
):
    compile_memory, _root, _state_root, _state = compile_env
    reconciliation = {
        "version": 1,
        "generation_lineage": [hashlib.sha256(b"predecessor").hexdigest()],
        "predecessor_journal_ids": predecessor_journal_ids,
        "journal_ids": [],
        "effects": [],
        "consumed_evidence": [],
    }

    with pytest.raises(ValueError):
        compile_memory._validate_manifest_legacy_reconciliation(reconciliation)


def test_legacy_reconciliation_rejects_effectless_id_as_effect_journal(compile_env):
    compile_memory, _root, _state_root, _state = compile_env
    journal_id = hashlib.sha256(b"effectless-journal").hexdigest()
    reconciliation = {
        "version": 1,
        "generation_lineage": [hashlib.sha256(b"predecessor").hexdigest()],
        "predecessor_journal_ids": [journal_id],
        "journal_ids": [journal_id],
        "effects": [],
        "consumed_evidence": [],
    }

    with pytest.raises(ValueError, match="effect journal"):
        compile_memory._validate_manifest_legacy_reconciliation(reconciliation)


def _run_compile_recovery_cli(
    vault: Path,
    state_root: Path,
    phase: str,
    manifest: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parent.parent / "scripts" / "compile_memory.py"
    command = [sys.executable, str(script), "--recover-pending", phase]
    if manifest is not None:
        command.extend(["--manifest", str(manifest)])
    env = os.environ.copy()
    env.update(
        {
            "LLM_WIKI_ROOT": str(vault),
            "LLM_WIKI_STATE_ROOT": str(state_root),
        }
    )
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _approve_recovery_manifest(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["approved"] = True
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _orphaned_pending_recovery_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    vault = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    daily = _daily(
        vault / "knowledge" / "daily" / "2026-07-01.md",
        [_block("12:00:00", "Recovery leaves this source unpublished.")],
    )
    state_path = state_root / "run" / "state.json"
    state_path.parent.mkdir(parents=True)
    state = {
        "compile_index_pending": {
            "batch_id": hashlib.sha256(b"missing-batch").hexdigest(),
            "daily": daily.name,
            "sha256": hashlib.sha256(daily.read_bytes()).hexdigest(),
            "generation_id": hashlib.sha256(b"missing-generation").hexdigest(),
        },
        "last_compile_status": "error",
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return vault, state_root, state_path, state


def test_pending_compile_recovery_is_manifest_gated_and_preserves_history(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    daily = _daily(
        vault / "knowledge" / "daily" / "2026-07-01.md",
        [_block("12:00:00", "Recovery leaves this source unpublished.")],
    )
    source_hash = hashlib.sha256(daily.read_bytes()).hexdigest()
    batch_id = hashlib.sha256(b"missing-batch").hexdigest()
    generation_id = hashlib.sha256(b"missing-generation").hexdigest()
    state_path = state_root / "run" / "state.json"
    state_path.parent.mkdir(parents=True)
    state = {
        "compile_index_pending": {
            "batch_id": batch_id,
            "daily": daily.name,
            "sha256": source_hash,
            "generation_id": generation_id,
        },
        "compiled_daily_hashes": {"sentinel.md": "a" * 64},
        "last_compile_at": "2026-07-31T05:49:10",
        "last_compile_status": "error",
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    before = state_path.read_bytes()

    audited = _run_compile_recovery_cli(vault, state_root, "audit")
    assert audited.returncode == 0, audited.stderr
    assert json.loads(audited.stdout)["status"] == "eligible"
    assert state_path.read_bytes() == before
    assert not (state_root / "run" / "backups").exists()

    prepared = _run_compile_recovery_cli(vault, state_root, "backup-only")
    assert prepared.returncode == 0, prepared.stderr
    prepared_result = json.loads(prepared.stdout)
    assert prepared_result["status"] == "prepared"
    manifest_path = Path(prepared_result["manifest"])
    assert manifest_path.is_file()
    assert state_path.read_bytes() == before
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["approved"] is False

    _approve_recovery_manifest(manifest_path)
    applied = _run_compile_recovery_cli(vault, state_root, "apply", manifest_path)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["status"] == "applied"
    current = json.loads(state_path.read_text(encoding="utf-8"))
    assert "compile_index_pending" not in current
    assert current["compiled_daily_hashes"] == state["compiled_daily_hashes"]
    assert current["last_compile_at"] == state["last_compile_at"]
    assert current["last_compile_status"] == "error"
    assert "daily remains uncompiled" in current["last_compile_error"]

    verified = _run_compile_recovery_cli(vault, state_root, "verify", manifest_path)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["status"] == "verified"


def test_pending_compile_recovery_fails_closed_when_artifact_appears(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    daily = _daily(vault / "knowledge" / "daily" / "2026-07-01.md", [])
    batch_id = hashlib.sha256(b"batch").hexdigest()
    generation_id = hashlib.sha256(b"generation").hexdigest()
    state_path = state_root / "run" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "compile_index_pending": {
                    "batch_id": batch_id,
                    "daily": daily.name,
                    "sha256": hashlib.sha256(daily.read_bytes()).hexdigest(),
                    "generation_id": generation_id,
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    journal = state_root / "run" / "compile-journal" / f"{batch_id}.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{}", encoding="utf-8")

    audited = _run_compile_recovery_cli(vault, state_root, "audit")
    assert audited.returncode == 3
    assert json.loads(audited.stdout)["status"] == "ineligible"
    assert state_path.read_bytes() == before
    assert journal.read_text(encoding="utf-8") == "{}"


def test_pending_compile_recovery_rejects_malformed_receipt_evidence(tmp_path):
    vault, state_root, state_path, state = _orphaned_pending_recovery_fixture(tmp_path)
    state["compiled_daily_receipts"] = {"other.md": []}
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    audited = _run_compile_recovery_cli(vault, state_root, "audit")

    assert audited.returncode == 3
    report = json.loads(audited.stdout)
    assert report["status"] == "ineligible"
    assert "compiled_receipts_schema_invalid" in report["diagnostics"]


def test_pending_compile_recovery_rejects_related_batch_progress(tmp_path):
    vault, state_root, state_path, state = _orphaned_pending_recovery_fixture(tmp_path)
    pending = state["compile_index_pending"]
    sibling_id = hashlib.sha256(b"sibling-batch").hexdigest()
    state["compile_sdk_progress"] = {
        pending["daily"]: {
            "generation_id": pending["generation_id"],
            "sha256": pending["sha256"],
            "completed_batch_ids": [sibling_id],
            "expected_batch_ids": [sibling_id, pending["batch_id"]],
        }
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    audited = _run_compile_recovery_cli(vault, state_root, "audit")

    assert audited.returncode == 3
    assert "related_compile_progress_present" in json.loads(audited.stdout)[
        "diagnostics"
    ]


def test_pending_compile_recovery_allows_unpublished_completion_history(tmp_path):
    vault, state_root, state_path, state = _orphaned_pending_recovery_fixture(tmp_path)
    pending = state["compile_index_pending"]
    expected = {pending["daily"]: pending["sha256"]}
    wave_id = hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state["compile_generation_completed"] = [pending["generation_id"]]
    state["compile_sdk_wave"] = {
        "version": 1,
        "wave_id": wave_id,
        "status": "active",
        "expected": expected,
        "completed": {},
        "daily_audits": {},
    }
    state["compile_daily_replay_boundaries"] = {
        pending["daily"]: {
            "version": 1,
            "daily_sha256": pending["sha256"],
            "generation_id": None,
            "journal_ids": [],
            "effects": [],
            "consumed_evidence": [],
            "requires_nonempty": True,
        }
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    audited = _run_compile_recovery_cli(vault, state_root, "audit")

    assert audited.returncode == 0, audited.stderr
    report = json.loads(audited.stdout)
    assert report["status"] == "eligible"
    assert report["observations"] == [
        "completed_generation_recorded",
        "active_compile_sdk_wave_recorded",
        "replay_boundary_preserved",
    ]


def test_pending_compile_recovery_verify_revalidates_state_backup(tmp_path):
    vault, state_root, _state_path, _state = _orphaned_pending_recovery_fixture(tmp_path)
    prepared = _run_compile_recovery_cli(vault, state_root, "backup-only")
    assert prepared.returncode == 0, prepared.stderr
    manifest_path = Path(json.loads(prepared.stdout)["manifest"])
    _approve_recovery_manifest(manifest_path)
    applied = _run_compile_recovery_cli(vault, state_root, "apply", manifest_path)
    assert applied.returncode == 0, applied.stderr

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_path = manifest_path.parent / manifest["state_backup"]
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    backup["compile_index_pending"]["batch_id"] = hashlib.sha256(
        b"substituted-backup"
    ).hexdigest()
    backup_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")

    verified = _run_compile_recovery_cli(vault, state_root, "verify", manifest_path)

    assert verified.returncode == 3
    assert "backup" in verified.stderr.lower()


def test_pending_compile_recovery_repairs_exact_missing_source_references(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    state_path = state_root / "run" / "state.json"
    state_path.parent.mkdir(parents=True)
    daily_name = "2026-07-01.md"
    source_hash = hashlib.sha256(b"missing daily source").hexdigest()
    generation_id = hashlib.sha256(b"completed generation").hexdigest()
    batch_id = hashlib.sha256(b"missing journal").hexdigest()
    expected = {daily_name: source_hash}
    state = {
        "compile_index_pending": {
            "batch_id": batch_id,
            "daily": daily_name,
            "sha256": source_hash,
            "generation_id": generation_id,
        },
        "compile_generation_completed": [generation_id],
        "compile_sdk_wave": {
            "version": 1,
            "wave_id": hashlib.sha256(
                json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "status": "active",
            "expected": expected,
            "completed": {},
            "daily_audits": {},
        },
        "compile_daily_replay_boundaries": {
            daily_name: {
                "version": 1,
                "daily_sha256": source_hash,
                "generation_id": None,
                "journal_ids": [],
                "effects": [],
                "requires_nonempty": True,
            }
        },
        "sentinel": {"preserve": True},
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    audited = _run_compile_recovery_cli(vault, state_root, "audit")
    assert audited.returncode == 0, audited.stderr
    report = json.loads(audited.stdout)
    assert report["status"] == "eligible"
    assert report["recovery_mode"] == "missing_source"
    assert report["daily_present"] is False

    prepared = _run_compile_recovery_cli(vault, state_root, "backup-only")
    assert prepared.returncode == 0, prepared.stderr
    manifest_path = Path(json.loads(prepared.stdout)["manifest"])
    _approve_recovery_manifest(manifest_path)
    applied = _run_compile_recovery_cli(vault, state_root, "apply", manifest_path)
    assert applied.returncode == 0, applied.stderr
    current = json.loads(state_path.read_text(encoding="utf-8"))
    assert "compile_index_pending" not in current
    assert "compile_sdk_wave" not in current
    assert "compile_daily_replay_boundaries" not in current
    assert current["compile_generation_completed"] == [generation_id]
    assert current["sentinel"] == state["sentinel"]
    assert "missing daily source" in current["last_compile_error"]


def test_pending_compile_recovery_missing_source_apply_rejects_source_appearance(
    tmp_path,
):
    vault = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    daily_dir = vault / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily_bytes = b"source reappeared after review\n"
    state_path = state_root / "run" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "compile_index_pending": {
                    "batch_id": hashlib.sha256(b"batch").hexdigest(),
                    "daily": "2026-07-01.md",
                    "sha256": hashlib.sha256(daily_bytes).hexdigest(),
                    "generation_id": hashlib.sha256(b"generation").hexdigest(),
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    prepared = _run_compile_recovery_cli(vault, state_root, "backup-only")
    assert prepared.returncode == 0, prepared.stderr
    manifest_path = Path(json.loads(prepared.stdout)["manifest"])
    _approve_recovery_manifest(manifest_path)
    (daily_dir / "2026-07-01.md").write_bytes(daily_bytes)

    applied = _run_compile_recovery_cli(vault, state_root, "apply", manifest_path)

    assert applied.returncode == 3
    assert "drift" in applied.stderr.lower()
    assert "compile_index_pending" in json.loads(
        state_path.read_text(encoding="utf-8")
    )


def test_pending_compile_recovery_missing_source_rejects_durable_boundary(tmp_path):
    vault = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    state_path = state_root / "run" / "state.json"
    state_path.parent.mkdir(parents=True)
    daily_name = "2026-07-01.md"
    source_hash = hashlib.sha256(b"missing source with evidence").hexdigest()
    journal_id = hashlib.sha256(b"durable journal").hexdigest()
    state_path.write_text(
        json.dumps(
            {
                "compile_index_pending": {
                    "batch_id": hashlib.sha256(b"missing batch").hexdigest(),
                    "daily": daily_name,
                    "sha256": source_hash,
                    "generation_id": hashlib.sha256(b"generation").hexdigest(),
                },
                "compile_daily_replay_boundaries": {
                    daily_name: {
                        "version": 1,
                        "daily_sha256": source_hash,
                        "generation_id": None,
                        "journal_ids": [journal_id],
                        "effects": [],
                        "consumed_evidence": [],
                        "requires_nonempty": True,
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    audited = _run_compile_recovery_cli(vault, state_root, "audit")

    assert audited.returncode == 3
    assert "replay_boundary_has_durable_evidence" in json.loads(audited.stdout)[
        "diagnostics"
    ]


def test_missing_source_post_state_completes_surviving_finished_wave():
    import compile_memory

    missing = "2026-07-01.md"
    sibling = "2026-07-02.md"
    missing_hash = hashlib.sha256(b"missing").hexdigest()
    sibling_hash = hashlib.sha256(b"sibling").hexdigest()
    pending = {
        "batch_id": hashlib.sha256(b"batch").hexdigest(),
        "daily": missing,
        "sha256": missing_hash,
        "generation_id": hashlib.sha256(b"generation").hexdigest(),
    }
    expected = {missing: missing_hash, sibling: sibling_hash}
    state = {
        "compile_index_pending": pending,
        "compile_sdk_wave": {
            "version": 1,
            "wave_id": compile_memory._sdk_wave_id(expected),
            "status": "active",
            "expected": expected,
            "completed": {sibling: sibling_hash},
            "daily_audits": {sibling: {}},
        },
    }

    post = compile_memory._pending_recovery_post_state(
        state,
        "missing_source",
        pending,
    )

    assert post["compile_sdk_wave"]["status"] == "complete"
    assert post["compile_sdk_wave"]["expected"] == {sibling: sibling_hash}
    assert post["compile_sdk_wave"]["wave_id"] == compile_memory._sdk_wave_id(
        {sibling: sibling_hash}
    )


def test_pending_compile_recovery_rejects_accepted_sibling_progress(tmp_path):
    vault, state_root, state_path, state = _orphaned_pending_recovery_fixture(tmp_path)
    pending = state["compile_index_pending"]
    state["compile_sdk_progress"] = {
        "other.md": {
            "generation_id": hashlib.sha256(b"other generation").hexdigest(),
            "sha256": hashlib.sha256(b"other source").hexdigest(),
            "completed_batch_ids": [],
            "expected_batch_ids": [],
            "accepted_batch_ids": [pending["batch_id"]],
        }
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    audited = _run_compile_recovery_cli(vault, state_root, "audit")

    assert audited.returncode == 3
    assert "related_compile_progress_present" in json.loads(audited.stdout)[
        "diagnostics"
    ]


def test_pending_compile_recovery_rejects_sibling_generation_artifact(tmp_path):
    vault, state_root, _state_path, state = _orphaned_pending_recovery_fixture(tmp_path)
    sibling_id = hashlib.sha256(b"sibling artifact").hexdigest()
    journal_dir = state_root / "run" / "compile-journal"
    journal_dir.mkdir()
    (journal_dir / f"{sibling_id}.json").write_text(
        json.dumps(
            {
                "batch_id": sibling_id,
                "accepted": {
                    "generation_id": state["compile_index_pending"]["generation_id"]
                },
            }
        ),
        encoding="utf-8",
    )

    audited = _run_compile_recovery_cli(vault, state_root, "audit")

    assert audited.returncode == 3
    assert "related_journal_artifact_present" in json.loads(audited.stdout)[
        "diagnostics"
    ]


def test_orphaned_legacy_audit_ignores_retained_current_artifacts(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    quote = "Current completed artifacts remain ordinary bounded history."
    current_daily = _daily(
        root / "knowledge" / "daily" / "2026-11-09-current-retained.md",
        [_block("12:00:00", quote)],
    )
    request = compile_memory.prepare_compile_request(
        [current_daily],
        state,
        prompt_char_budget=30_000,
    )
    result = compile_memory.apply_compile_batch(
        request,
        _response(_admission_operation(current_daily, "current-retained", quote)),
        False,
    )
    assert result["ok"] is True
    assert result["daily_complete"] is True
    assert request["generation_id"] in state["compile_generation_completed"]

    legacy_daily = _daily(
        root / "knowledge" / "daily" / "2026-11-10-orphaned-empty.md",
        [],
    )
    legacy = _legacy_v2_manifest(compile_memory, legacy_daily)
    assert legacy["batch_ids"] == []
    compile_memory._write_new_manifest(legacy)

    def state_snapshot():
        raw = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        return raw, json.loads(json.dumps(state))

    monkeypatch.setattr(
        compile_memory,
        "read_state_snapshot_strict",
        state_snapshot,
    )

    audited = compile_memory.recover_orphaned_generations("audit")

    assert audited["status"] == "eligible"
    assert audited["manifest_ids"] == [legacy["generation_id"]]
    assert audited["journal_ids"] == []
    assert [(item["kind"], item["id"]) for item in audited["artifacts"]] == [
        ("manifest", legacy["generation_id"])
    ]
    assert compile_memory._manifest_path(request["generation_id"]).is_file()
    assert compile_memory._journal_path(request["batch_id"]).is_file()


def test_orphaned_legacy_generation_recovery_preserves_effect_provenance(
    compile_env,
    monkeypatch,
):
    compile_memory, root, state_root, state = compile_env
    quote = "Always retain orphaned legacy provenance before releasing capacity."
    legacy_noise = (
        "## [11:59:00] opencode-heartbeat | legacy-noise\n"
        "- Status: transient heartbeat only"
    )
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-10-orphaned-legacy.md",
        [
            legacy_noise,
            "## [12:00:00] opencode-idle | legacy-session\n"
            "- Tier: `major`\n\n"
            f"**Lessons / patterns**\n- {quote}"
        ],
    )
    current_extract = compile_memory._extract_legacy_manifest_blocks

    def legacy_extract(value):
        return [
            legacy_noise,
            *[
                block.replace("\n\n", "\n- Tier: `major`\n\n", 1)
                for block in current_extract(value)
            ],
        ]

    monkeypatch.setattr(
        compile_memory,
        "_extract_legacy_manifest_blocks",
        legacy_extract,
    )
    legacy = _legacy_v2_manifest(compile_memory, daily)
    old_request = _activate_legacy_manifest(compile_memory, state, daily, legacy)
    monkeypatch.setattr(
        compile_memory,
        "_extract_legacy_manifest_blocks",
        current_extract,
    )
    operation = _shipped_v1_operation(daily, "orphaned-legacy", quote)
    operation["evidence"].append(
        {
            **operation["evidence"][0],
            "claim": "A second legacy claim may share the same wildcard timestamp.",
        }
    )
    marker = compile_memory._operation_marker(old_request, 0, operation)
    fingerprint = compile_memory._operation_replay_fingerprint(operation)
    target = root / "knowledge" / "notes" / "orphaned-legacy.md"
    target.write_text(
        _render_shipped_v1_create(
            compile_memory,
            operation,
            marker,
            fingerprint,
        ),
        encoding="utf-8",
    )
    snapshot = compile_memory._read_knowledge_page_snapshot(target)
    assert snapshot is not None
    _write_legacy_v1_journal(
        compile_memory,
        old_request,
        [operation],
        states=["applied"],
        effects=[
            {
                "version": 2,
                "target": target.name,
                "before": None,
                "after": snapshot[1],
                "retained_artifact": None,
            }
        ],
        status="complete",
    )
    state.pop("compile_generation_active")

    def state_snapshot():
        raw = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        return raw, json.loads(json.dumps(state))

    def update_state_if_unchanged(expected_raw, mutator):
        current_raw, _current = state_snapshot()
        if current_raw != expected_raw:
            raise ValueError("test state preimage changed")
        mutator(state)
        published_raw, published = state_snapshot()
        return published_raw, published

    monkeypatch.setattr(
        compile_memory,
        "read_state_snapshot_strict",
        state_snapshot,
    )
    monkeypatch.setattr(
        compile_memory,
        "update_state_if_unchanged",
        update_state_if_unchanged,
    )
    target_before = target.read_bytes()

    audited = compile_memory.recover_orphaned_generations("audit")
    assert audited["status"] == "eligible"
    assert audited["manifest_ids"] == [legacy["generation_id"]]
    assert audited["journal_ids"] == [old_request["batch_id"]]

    prepared = compile_memory.recover_orphaned_generations("backup-only")
    manifest_path = Path(prepared["manifest"])
    with pytest.raises(ValueError, match="not approved"):
        compile_memory.recover_orphaned_generations("apply", manifest_path)
    assert compile_memory._manifest_path(legacy["generation_id"]).is_file()
    assert compile_memory._journal_path(old_request["batch_id"]).is_file()
    _approve_recovery_manifest(manifest_path)
    state["tool_capture_dedupe"] = {
        "v1:" + "a" * 64: "2026-11-10T12:01:00",
    }
    applied = compile_memory.recover_orphaned_generations(
        "apply",
        manifest_path,
    )
    verified = compile_memory.recover_orphaned_generations(
        "verify",
        manifest_path,
    )

    assert applied["status"] == "applied"
    assert verified["status"] == "verified"
    assert target.read_bytes() == target_before
    assert not compile_memory._manifest_path(legacy["generation_id"]).exists()
    assert not compile_memory._journal_path(old_request["batch_id"]).exists()
    assert compile_memory._load_manifest_for_orphan_recovery(
        legacy["generation_id"]
    )["version"] == 2
    assert compile_memory._load_journal(old_request["batch_id"])["status"] == "complete"
    persisted = compile_memory.load_state()
    assert persisted["tool_capture_dedupe"] == state["tool_capture_dedupe"]
    reconciliation = persisted["compile_legacy_reconciliations"][daily.name]
    assert reconciliation["generation_lineage"] == [legacy["generation_id"]]
    assert reconciliation["predecessor_journal_ids"] == [old_request["batch_id"]]
    assert reconciliation["journal_ids"] == [old_request["batch_id"]]
    assert reconciliation["effects"][0]["target"] == target.name
    assert reconciliation["effects"][0]["marker"] == marker
    assert reconciliation["effects"][0]["fingerprint"] == fingerprint
    assert persisted["compile_consumed_evidence"] == [
        compile_memory._evidence_wildcard_token(operation["evidence"][0])
    ]
    assert (state_root / "run" / "backups" / manifest_path.parent.name).is_dir()

    old_source_sha256 = legacy["daily"]["sha256"]
    _daily(daily, [])
    current_source_sha256 = hashlib.sha256(daily.read_bytes()).hexdigest()
    state["compile_daily_replay_boundaries"] = {
        daily.name: {
            "version": 1,
            "daily_sha256": old_source_sha256,
            "generation_id": None,
            "journal_ids": [],
            "effects": [],
            "consumed_evidence": [],
            "requires_nonempty": True,
        }
    }
    with pytest.raises(
        compile_memory.CompilePreparationError,
        match="does not satisfy replay boundary",
    ):
        compile_memory.prepare_compile_request(
            [daily],
            state,
            prompt_char_budget=30_000,
        )
    effectful_pending = json.loads(json.dumps(state["compile_index_pending"]))

    effectful_audit = compile_memory.recover_empty_replay_boundary("audit")
    assert effectful_audit["status"] == "eligible"
    effectful_prepared = compile_memory.recover_empty_replay_boundary("backup-only")
    effectful_manifest_path = Path(effectful_prepared["manifest"])
    _approve_recovery_manifest(effectful_manifest_path)
    assert compile_memory.recover_empty_replay_boundary(
        "apply", effectful_manifest_path
    ) == {"status": "applied"}
    assert compile_memory.recover_empty_replay_boundary(
        "verify", effectful_manifest_path
    ) == {"status": "verified"}
    advanced_boundary = state["compile_daily_replay_boundaries"][daily.name]
    assert advanced_boundary["daily_sha256"] == current_source_sha256
    assert advanced_boundary["requires_nonempty"] is True
    assert state["compile_index_pending"] == effectful_pending
    effectful_resumed = compile_memory._resume_pending_index_if_any()
    assert effectful_resumed is not None and effectful_resumed["ok"] is True
    assert state["compiled_daily_receipts"][daily.name]["effects"][0]["target"] == (
        target.name
    )


def test_empty_replay_boundary_recovery_requires_reviewed_zero_evidence(
    compile_env,
    monkeypatch,
):
    compile_memory, root, _state_root, state = compile_env
    daily = _daily(
        root / "knowledge" / "daily" / "2026-11-11-empty-boundary.md",
        [],
    )
    source_sha256 = hashlib.sha256(daily.read_bytes()).hexdigest()
    state["compile_daily_replay_boundaries"] = {
        daily.name: {
            "version": 1,
            "daily_sha256": source_sha256,
            "generation_id": None,
            "journal_ids": [],
            "effects": [],
            "consumed_evidence": [],
            "requires_nonempty": True,
        }
    }
    state["compile_legacy_reconciliations"] = {
        daily.name: {
            "version": 1,
            "generation_lineage": ["d" * 64],
            "predecessor_journal_ids": [],
            "journal_ids": [],
            "effects": [],
            "consumed_evidence": [],
        }
    }

    def state_snapshot():
        raw = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        return raw, json.loads(json.dumps(state))

    def update_state_if_unchanged(expected_raw, mutator):
        current_raw, _current = state_snapshot()
        if current_raw != expected_raw:
            raise ValueError("test state preimage changed")
        mutator(state)
        published_raw, published = state_snapshot()
        return published_raw, published

    monkeypatch.setattr(
        compile_memory,
        "read_state_snapshot_strict",
        state_snapshot,
    )
    monkeypatch.setattr(
        compile_memory,
        "update_state_if_unchanged",
        update_state_if_unchanged,
    )

    with pytest.raises(
        compile_memory.CompilePreparationError,
        match="does not satisfy replay boundary",
    ):
        compile_memory.prepare_compile_request(
            [daily],
            state,
            prompt_char_budget=30_000,
        )
    pending_before = json.loads(json.dumps(state["compile_index_pending"]))
    other_daily = _daily(
        root / "knowledge" / "daily" / "2026-11-12-other-empty.md",
        [],
    )
    other_manifest = compile_memory._create_generation_manifest(
        other_daily,
        30_000,
        state,
    )
    state["compile_generation_active"].pop(other_daily.name)
    state.setdefault("compile_generation_completed", []).append(
        other_manifest["generation_id"]
    )

    state["compile_daily_replay_boundaries"][daily.name]["journal_ids"] = [
        "c" * 64
    ]
    rejected = compile_memory.recover_empty_replay_boundary("audit")
    assert rejected["status"] == "ineligible"
    assert "empty_boundary_has_durable_or_changed_evidence" in rejected["diagnostics"]
    state["compile_daily_replay_boundaries"][daily.name]["journal_ids"] = []
    audited = compile_memory.recover_empty_replay_boundary("audit")
    assert audited == {
        "status": "eligible",
        "eligible": True,
        "diagnostics": [],
        "daily": daily.name,
        "generation_id": pending_before["generation_id"],
        "state_before_sha256": audited["state_before_sha256"],
        "state_after_sha256": audited["state_after_sha256"],
    }
    prepared = compile_memory.recover_empty_replay_boundary("backup-only")
    manifest_path = Path(prepared["manifest"])
    with pytest.raises(ValueError, match="not approved"):
        compile_memory.recover_empty_replay_boundary("apply", manifest_path)
    _approve_recovery_manifest(manifest_path)
    state["tool_capture_dedupe"] = {
        "v1:" + "b" * 64: "2026-11-11T12:01:00",
    }

    assert compile_memory.recover_empty_replay_boundary(
        "apply", manifest_path
    ) == {"status": "applied"}
    assert compile_memory.recover_empty_replay_boundary(
        "verify", manifest_path
    ) == {"status": "verified"}
    assert state["compile_index_pending"] == pending_before
    assert (
        state["compile_daily_replay_boundaries"][daily.name]["requires_nonempty"]
        is False
    )
    resumed = compile_memory._resume_pending_index_if_any()
    assert resumed is not None and resumed["ok"] is True
    assert "compile_index_pending" not in state
    assert state["compiled_daily_hashes"][daily.name] == source_sha256
    assert state["compiled_daily_receipts"][daily.name]["effects"] == []


def test_pending_compile_recovery_publication_cas_uses_manifest_preimage(monkeypatch):
    import compile_memory

    backup_raw = b'{"compile_index_pending":{}}'
    expected_post = {"last_compile_status": "error"}
    captured = {}

    def guarded_update(expected_raw, mutator):
        captured["expected_raw"] = expected_raw
        state = {"compile_index_pending": {}}
        mutator(state)
        return json.dumps(state).encode(), state

    monkeypatch.setattr(compile_memory, "update_state_if_unchanged", guarded_update)

    _published_raw, published = compile_memory._publish_pending_recovery_state(
        backup_raw,
        backup_raw,
        expected_post,
    )
    assert captured["expected_raw"] == backup_raw
    assert published == expected_post
    with pytest.raises(ValueError, match="drifted during evidence review"):
        compile_memory._publish_pending_recovery_state(
            backup_raw,
            b'{"concurrent_sentinel":true}',
            expected_post,
        )
