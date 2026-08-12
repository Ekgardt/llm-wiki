from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "repair_installed_memory.py"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _snapshot(root: Path) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            result[relative] = ("symlink", os.readlink(path))
        elif stat.S_ISDIR(metadata.st_mode):
            result[relative] = ("dir", "")
        elif stat.S_ISREG(metadata.st_mode):
            result[relative] = ("file", _sha(path.read_bytes()))
        else:
            result[relative] = ("special", str(stat.S_IFMT(metadata.st_mode)))
    return result


def _run(
    mode: str,
    vault: Path,
    state: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            mode,
            "--root",
            str(vault),
            "--state-root",
            str(state),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        pytest.fail(f"repair command failed: {result.stderr}\n{result.stdout}")
    return result


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes").mkdir(parents=True)
    (vault / "knowledge" / "feedback").mkdir(parents=True)
    return vault, state


def _stale_args(*relative_paths: str) -> tuple[str, ...]:
    return tuple(value for path in relative_paths for value in ("--stale-page", path))


def _audit_file(
    vault: Path,
    state: Path,
    *stale_pages: str,
    name: str = "audit.json",
) -> Path:
    output = vault.parent / name
    _run(
        "audit",
        vault,
        state,
        *_stale_args(*stale_pages),
        "--output",
        str(output),
    )
    return output


def _prepare(
    vault: Path,
    state: Path,
    *stale_pages: str,
) -> tuple[Path, Path, dict]:
    audit = _audit_file(vault, state, *stale_pages)
    result = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit),
        "--backup-only",
        *_stale_args(*stale_pages),
    )
    report = json.loads(result.stdout)
    return audit, Path(report["backup_manifest"]), report


def _approve_manifest(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["approved"] is False
    manifest["approved"] = True
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def _apply_manifest(
    vault: Path,
    state: Path,
    audit: Path,
    manifest: Path,
    *stale_pages: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit),
        "--manifest",
        str(manifest),
        *_stale_args(*stale_pages),
        check=check,
    )


def _feedback_record(**overrides) -> dict:
    record = {
        "id": "a" * 12,
        "type": "correction",
        "confidence": 0.7,
        "text": "Generated correction candidate.",
        "session_id": "service-session",
        "project": "demo",
        "trigger": "opencode-idle",
        "captured_at": "2026-08-03T12:00:00",
        "status": "candidate",
        "source_role": "service",
    }
    record.update(overrides)
    return record


def _project_state(slug: str, project_root: Path, handoff: str) -> str:
    return (
        f"# {slug} state\n"
        f"- Project root JSON: {json.dumps(str(project_root.resolve()))}\n"
        f"- Runtime slug JSON: {json.dumps(slug)}\n\n"
        "## Where we left off\n"
        f"{handoff}\n\n"
        "## Open threads\n"
        "- Preserve this section exactly.\n"
    )


def _write_manifest(manifest_path: Path, manifest: dict, *, reseal: bool = False) -> None:
    import repair_installed_memory as repair

    manifest_path.write_bytes(repair._json_bytes(manifest))
    if reseal:
        seal_path = manifest_path.with_name("manifest.seal.json")
        try:
            seal_path.chmod(0o600)
        except OSError:
            pass
        seal_path.write_bytes(
            repair._json_bytes(
                {
                    "schema_version": 4,
                    "sealed_manifest_sha256": _sha(
                        repair._manifest_sealed_bytes(manifest)
                    ),
                }
            )
        )


def _actionable_fixture(tmp_path: Path) -> dict:
    from session_start_project_state import STATE_SECTION_TEMPLATE_PLACEHOLDERS

    vault, state = _vault(tmp_path)
    placeholder = STATE_SECTION_TEMPLATE_PLACEHOLDERS["where we left off"]
    exact = "# beta\n\nByte-exact body.\n"
    canonical = _write(vault / "knowledge" / "notes" / "alpha.md", exact)
    shadow = _write(vault / "knowledge" / "notes" / "typed" / "beta.md", exact)
    nonidentical = _write(
        vault / "knowledge" / "notes" / "gamma.md",
        "# alpha\n\nDistinct body.\n",
    )
    stale_relative = "knowledge/notes/stale.md"
    stale = _write(vault / stale_relative, "# Reviewed stale\n\nObsolete.\n")
    generated_feedback = _write(
        vault / "knowledge" / "feedback" / "generated.json",
        json.dumps(_feedback_record()),
    )
    user_feedback = _write(
        vault / "knowledge" / "feedback" / "user.json",
        json.dumps(
            _feedback_record(
                id="b" * 12,
                trigger="opencode-user-message",
                source_role="user",
            )
        ),
    )
    generated_daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    ordinary_daily = _write(
        vault / "knowledge" / "daily" / "2026-08-04.md",
        "# Daily 2026-08-04\nA durable user decision remains.\n",
    )
    project_root = tmp_path / "worktrees" / "trusted"
    project_root.mkdir(parents=True)
    project_state = _write(
        vault / "knowledge" / "projects" / "trusted" / "state.md",
        _project_state("trusted", project_root, placeholder),
    )
    hidden_root = tmp_path / "worktrees" / "hidden"
    hidden_root.mkdir(parents=True)
    hidden_state = _write(
        vault / "knowledge" / "projects" / "hidden" / "state.md",
        _project_state("hidden", hidden_root, f"<!-- {placeholder} -->"),
    )
    retained = {
        path: path.read_bytes()
        for path in (
            canonical,
            nonidentical,
            user_feedback,
            ordinary_daily,
            hidden_state,
        )
    }
    return {
        "vault": vault,
        "state": state,
        "placeholder": placeholder,
        "canonical": canonical,
        "shadow": shadow,
        "stale": stale,
        "stale_relative": stale_relative,
        "generated_feedback": generated_feedback,
        "generated_daily": generated_daily,
        "project_state": project_state,
        "project_before": project_state.read_bytes(),
        "retained": retained,
    }


def _crash_v4_after_first_action(vault: Path, state: Path, audit: Path) -> Path:
    prepared = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit),
        "--backup-only",
    )
    manifest = Path(json.loads(prepared.stdout)["backup_manifest"])
    _approve_manifest(manifest)
    code = f"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import repair_installed_memory as repair
vault = Path({str(vault)!r})
state = Path({str(state)!r})
audit = Path({str(audit)!r})
audit_bytes = audit.read_bytes()
report = json.loads(audit_bytes)
manifest = Path({str(manifest)!r})
real = repair._v4_commit_entry
def crash(entry, manifest_path, root):
    real(entry, manifest_path, root)
    os._exit(91)
repair._v4_commit_entry = crash
repair.apply_repair(report, audit_bytes, vault, state, manifest, backup_only=False)
"""
    crashed = subprocess.run([sys.executable, "-c", code], cwd=ROOT)
    assert crashed.returncode == 91
    return manifest


def _crash_v4_preparation(
    vault: Path,
    state: Path,
    audit: Path,
    *,
    after_first_staging_write: bool,
) -> Path:
    code = f"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import repair_installed_memory as repair
vault = Path({str(vault)!r})
state = Path({str(state)!r})
audit = Path({str(audit)!r})
audit_bytes = audit.read_bytes()
report = json.loads(audit_bytes)
real = repair._durable_new_file
def crash(path, data, root):
    if path.suffix == '.source':
        if {after_first_staging_write!r}:
            real(path, data, root)
        os._exit(93)
    return real(path, data, root)
repair._durable_new_file = crash
repair.create_backup(report, audit_bytes, vault, state)
"""
    crashed = subprocess.run([sys.executable, "-c", code], cwd=ROOT)
    assert crashed.returncode == 93
    transaction_dirs = list((state / "run" / "backups").iterdir())
    assert len(transaction_dirs) == 1
    return transaction_dirs[0]


def _interrupt_v4_rollback_before_purge(tmp_path: Path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    files = [
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
        for day in (2, 3)
    ]
    originals = {path: path.read_bytes() for path in files}
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest)
    real_commit = repair._v4_commit_entry
    real_purge = repair._purge_v4_source_staging
    calls = 0

    def fail_second(entry, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-action failure")
        return real_commit(entry, *args, **kwargs)

    def interrupt_purge(*_args, **_kwargs):
        raise OSError("injected pre-purge interruption")

    monkeypatch.setattr(repair, "_v4_commit_entry", fail_second)
    monkeypatch.setattr(repair, "_purge_v4_source_staging", interrupt_purge)
    with pytest.raises(repair.TransactionError):
        repair.apply_repair(
            report,
            audit_bytes,
            vault,
            state,
            manifest,
            backup_only=False,
        )
    monkeypatch.setattr(repair, "_purge_v4_source_staging", real_purge)
    return vault, state, manifest, originals


def _interrupted_v3_transaction(
    vault: Path,
    state: Path,
    source: Path,
    original: bytes,
    staged: bytes,
) -> Path:
    import repair_installed_memory as repair

    transaction_dir = state / "run" / "backups" / "20260726T120000.000000Z"
    relative = source.relative_to(vault).as_posix()
    path_id = repair._opaque_path_id(vault, source, "daily")
    backup_relative = f"files/{relative}"
    staged_relative = f"staged/{relative}"
    backup = transaction_dir / backup_relative
    staged_path = transaction_dir / staged_relative
    backup.parent.mkdir(parents=True)
    staged_path.parent.mkdir(parents=True)
    backup.write_bytes(original)
    staged_path.write_bytes(staged)
    candidate = {
        "id": f"daily_noise:{path_id}",
        "kind": "daily_noise",
        "path_id": path_id,
        "action": "clean_daily",
        "before_sha256": _sha(original),
        "after_sha256": _sha(staged),
        "reason": "legacy v3 fixture",
        "status": "candidate",
        "metadata": {},
    }
    audit_digest = "a" * 64
    manifest = {
        "schema_version": 3,
        "status": "complete",
        "approved": True,
        "created_at": "2026-07-26T12:00:00+00:00",
        "vault_root": str(vault),
        "state_root": str(state),
        "audit_report_sha256": audit_digest,
        "files": [
            {
                "path": relative,
                "path_id": path_id,
                "action": "clean_daily",
                "sha256": _sha(original),
                "size": len(original),
                "backup_path": backup_relative,
                "staged_path": staged_relative,
                "staged_sha256": _sha(staged),
                "staged_size": len(staged),
            }
        ],
        "candidates": [candidate],
    }
    manifest_path = transaction_dir / "manifest.json"
    manifest_path.write_bytes(repair._json_bytes(manifest))
    manifest_path.with_name("transaction.json").write_bytes(
        repair._json_bytes(
            {
                "schema_version": 3,
                "audit_report_sha256": audit_digest,
                "status": "committing",
                "attempted_path_ids": [path_id],
                "mutated_path_ids": [],
                "restored_path_ids": [],
                "attempted_paths": [relative],
                "mutated_paths": [],
                "restored_paths": [],
                "commit_error": None,
                "rollback_errors": [],
            }
        )
    )
    return manifest_path


def _write_v3_report_manifest(
    vault: Path,
    state: Path,
    candidates: list[dict],
    *,
    audit_digest: str = "a" * 64,
    diagnostics: list[dict] | None = None,
    stale_pages: list[str] | None = None,
) -> Path:
    import repair_installed_memory as repair

    transaction_dir = state / "run" / "backups" / "20260726T120000.000000Z"
    transaction_dir.mkdir(parents=True)
    manifest_path = transaction_dir / "manifest.json"
    manifest_path.write_bytes(
        repair._json_bytes(
            {
                "schema_version": 3,
                "status": "complete",
                "approved": True,
                "created_at": "2026-07-26T12:00:00+00:00",
                "vault_root": str(vault),
                "state_root": str(state),
                "audit_report_sha256": audit_digest,
                "files": [],
                "candidates": candidates,
                "diagnostics": diagnostics or [],
                "stale_pages": stale_pages or [],
            }
        )
    )
    return manifest_path


def test_audit_stdout_is_schema_v4_json_and_read_only(tmp_path):
    vault, state = _vault(tmp_path)
    _write(vault / "knowledge" / "daily" / "2026-08-03.md", "# Daily 2026-08-03\n")
    before_vault = _snapshot(vault)
    before_state = _snapshot(state)

    result = _run("audit", vault, state)

    report = json.loads(result.stdout)
    assert result.stderr == ""
    assert result.stdout.endswith("\n")
    assert set(report) == {
        "schema_version",
        "mode",
        "status",
        "root_fingerprint",
        "stale_pages",
        "backup_manifest",
        "candidates",
        "diagnostics",
        "summary",
    }
    assert report["schema_version"] == 4
    assert report["mode"] == "audit"
    assert report["stale_pages"] == []
    assert report["backup_manifest"] is None
    assert _snapshot(vault) == before_vault
    assert _snapshot(state) == before_state


def test_audit_writes_only_explicit_safe_output(tmp_path):
    vault, state = _vault(tmp_path)
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    output = output_dir / "audit.json"

    result = _run("audit", vault, state, "--output", str(output))

    assert result.stdout == ""
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "audit"
    assert not state.exists()


def test_audit_rejects_unsafe_output_before_inventory(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    inventory_called = False

    def inventory_must_not_run(*_args, **_kwargs):
        nonlocal inventory_called
        inventory_called = True
        raise AssertionError("inventory ran before output preflight")

    monkeypatch.setattr(repair, "inventory", inventory_must_not_run)

    result = repair.main(
        [
            "audit",
            "--root",
            str(vault),
            "--state-root",
            str(state),
            "--output",
            str(vault / "audit.json"),
        ]
    )

    assert result == 2
    assert inventory_called is False
    assert not (vault / "audit.json").exists()


@pytest.mark.parametrize("location", ("vault", "state"))
def test_apply_rejects_output_in_private_roots_before_staging(tmp_path, location):
    vault, state = _vault(tmp_path)
    _write(vault / "knowledge" / "daily" / "2026-08-03.md", "# Daily 2026-08-03\n")
    audit = _audit_file(vault, state)
    output = (vault if location == "vault" else state) / "apply.json"

    result = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit),
        "--backup-only",
        "--output",
        str(output),
        check=False,
    )

    assert result.returncode != 0
    assert not output.exists()
    assert not state.exists()


@pytest.mark.parametrize("alias_kind", ("same-path", "hard-link"))
def test_apply_rejects_output_alias_of_audit_before_staging(tmp_path, alias_kind):
    vault, state = _vault(tmp_path)
    _write(vault / "knowledge" / "daily" / "2026-08-03.md", "# Daily 2026-08-03\n")
    audit = _audit_file(vault, state)
    original = audit.read_bytes()
    output = audit
    if alias_kind == "hard-link":
        output = tmp_path / "audit-hard-link.json"
        try:
            os.link(audit, output)
        except OSError:
            pytest.skip("hard links unavailable")

    result = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit),
        "--backup-only",
        "--output",
        str(output),
        check=False,
    )

    assert result.returncode != 0
    assert audit.read_bytes() == original
    assert not state.exists()


def test_audit_targets_only_byte_exact_selector_shadows_in_transitive_group(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    exact = "# beta\n\nByte-exact body.\n"
    canonical = _write(vault / "knowledge" / "notes" / "alpha.md", exact)
    shadow = _write(vault / "knowledge" / "notes" / "typed" / "beta.md", exact)
    nonidentical = _write(
        vault / "knowledge" / "notes" / "gamma.md",
        "# alpha\n\nDifferent body that must remain.\n",
    )

    report = json.loads(_run("audit", vault, state).stdout)

    assert [item["action"] for item in report["candidates"]] == [
        "delete_exact_duplicate_note"
    ]
    assert report["candidates"][0]["path_id"] == repair._opaque_path_id(
        vault, shadow, "note"
    )
    assert all(
        item["path_id"] != repair._opaque_path_id(vault, canonical, "note")
        for item in report["candidates"]
    )
    assert {item["kind"] for item in report["diagnostics"]} == {
        "semantic_duplicate_note"
    }
    assert canonical.read_text(encoding="utf-8") == exact
    assert shadow.read_text(encoding="utf-8") == exact
    assert nonidentical.read_text(encoding="utf-8").endswith(
        "Different body that must remain.\n"
    )


def test_nonidentical_duplicate_diagnostic_is_single_and_bounded(tmp_path):
    vault, state = _vault(tmp_path)
    for index in range(300):
        _write(
            vault / "knowledge" / "notes" / f"private-{index:03d}.md",
            "# Shared private title\n\n" f"Distinct body {index}.\n",
        )

    result = _run("audit", vault, state)
    report = json.loads(result.stdout)
    groups = [
        item for item in report["diagnostics"] if item["kind"] == "semantic_duplicate_note"
    ]

    assert len(groups) == 1
    assert groups[0]["metadata"]["member_count"] == 300
    assert report["candidates"] == []
    assert "Shared private title" not in result.stdout
    assert "private-000" not in result.stdout
    assert len(result.stdout.encode("utf-8")) < 35_000


def test_audit_accepts_explicit_exact_stale_canonical_note(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    stale = _write(
        vault / "knowledge" / "notes" / "stale.md",
        "# Explicit stale page\n\nReviewed obsolete content.\n",
    )

    report = json.loads(
        _run(
            "audit",
            vault,
            state,
            "--stale-page",
            "knowledge/notes/stale.md",
        ).stdout
    )

    assert report["stale_pages"] == ["knowledge/notes/stale.md"]
    assert [item["action"] for item in report["candidates"]] == ["delete_stale_note"]
    assert report["candidates"][0]["path_id"] == repair._opaque_path_id(
        vault, stale, "note"
    )


@pytest.mark.parametrize(
    "invalid_kind",
    ("absent", "unsafe", "case-alias", "shadow", "editorial", "archive"),
)
def test_audit_rejects_noncanonical_or_unsafe_stale_page(tmp_path, invalid_kind):
    vault, state = _vault(tmp_path)
    notes = vault / "knowledge" / "notes"
    relative = "knowledge/notes/missing.md"
    protected = []
    if invalid_kind == "unsafe":
        relative = "knowledge/notes/../queue/private.md"
    elif invalid_kind == "case-alias":
        protected.append(_write(notes / "Stale.md", "# Exact stale\n\nBody.\n"))
        relative = "knowledge/notes/stale.md"
    elif invalid_kind == "shadow":
        exact = "# Shared\n\nExact bytes.\n"
        protected.append(_write(notes / "shared.md", exact))
        protected.append(_write(notes / "typed" / "shadow.md", exact))
        relative = "knowledge/notes/typed/shadow.md"
    elif invalid_kind == "editorial":
        protected.append(_write(notes / "README.md", "# Editorial\n"))
        relative = "knowledge/notes/README.md"
    elif invalid_kind == "archive":
        protected.append(_write(notes / "archive" / "old.md", "# Archived\n"))
        relative = "knowledge/notes/archive/old.md"
    before = {path: path.read_bytes() for path in protected}

    result = _run(
        "audit",
        vault,
        state,
        "--stale-page",
        relative,
        check=False,
    )

    assert result.returncode != 0
    assert "stale" in result.stderr.casefold()
    assert {path: path.read_bytes() for path in protected} == before
    assert not state.exists()


@pytest.mark.parametrize("alias", ("same", "case"))
def test_audit_rejects_duplicate_or_case_alias_stale_inputs(tmp_path, alias):
    vault, state = _vault(tmp_path)
    note = _write(
        vault / "knowledge" / "notes" / "Stale.md",
        "# Explicit stale\n\nBody.\n",
    )
    second = "knowledge/notes/Stale.md" if alias == "same" else "knowledge/notes/stale.md"

    result = _run(
        "audit",
        vault,
        state,
        "--stale-page",
        "knowledge/notes/Stale.md",
        "--stale-page",
        second,
        check=False,
    )

    assert result.returncode != 0
    assert "duplicate" in result.stderr.casefold() or "alias" in result.stderr.casefold()
    assert note.exists()


def test_audit_never_infers_staleness_from_age(tmp_path):
    vault, state = _vault(tmp_path)
    note = _write(
        vault / "knowledge" / "notes" / "very-old.md",
        "# Old but active\n\nAge alone is not deletion evidence.\n",
    )
    os.utime(note, (1, 1))

    report = json.loads(_run("audit", vault, state).stdout)

    assert report["stale_pages"] == []
    assert all(item["action"] != "delete_stale_note" for item in report["candidates"])
    assert note.exists()


def test_audit_targets_only_canonical_generated_service_feedback(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    feedback = vault / "knowledge" / "feedback"
    generated = _write(feedback / "generated.json", json.dumps(_feedback_record()))
    direct = _write(
        feedback / "user.json",
        json.dumps(
            _feedback_record(
                id="b" * 12,
                trigger="opencode-user-message",
                source_role="user",
            )
        ),
    )
    promoted = _write(
        feedback / "promoted.json",
        json.dumps(_feedback_record(id="c" * 12, status="promoted")),
    )
    rejected = _write(
        feedback / "rejected.json",
        json.dumps(_feedback_record(id="d" * 12, status="rejected")),
    )
    malformed = _write(feedback / "malformed.json", '{"status":"candidate"}')
    before = {path: path.read_bytes() for path in feedback.glob("*.json")}

    report = json.loads(_run("audit", vault, state).stdout)

    assert [item["action"] for item in report["candidates"]] == [
        "delete_false_feedback"
    ]
    assert report["candidates"][0]["path_id"] == repair._opaque_path_id(
        vault, generated, "feedback"
    )
    diagnostics = [
        item for item in report["diagnostics"] if item["kind"] == "feedback_preserved"
    ]
    assert {item["metadata"]["classification"] for item in diagnostics} == {
        "direct_user",
        "promoted",
        "rejected",
        "malformed",
    }
    assert {path: path.read_bytes() for path in before} == before
    assert all(path.exists() for path in (direct, promoted, rejected, malformed))


def test_audit_targets_feedback_shape_written_by_idle_producer(tmp_path, monkeypatch):
    import feedback_capture
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)

    @contextmanager
    def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", vault / "knowledge" / "feedback")
    monkeypatch.setattr(feedback_capture, "feedback_writer_lock", unlocked)
    candidate_id = feedback_capture.capture_from_text(
        "Actually, use the canonical writer-generated feedback shape.",
        session_id="service-session",
        slug="demo",
        trigger="opencode-idle",
    )
    assert candidate_id is not None
    generated = vault / "knowledge" / "feedback" / f"{candidate_id}.json"
    produced = json.loads(generated.read_text(encoding="utf-8"))
    assert produced["status"] == "candidate"
    assert produced["trigger"] == "opencode-idle"
    assert "source_role" not in produced

    report = json.loads(_run("audit", vault, state).stdout)

    assert [item["action"] for item in report["candidates"]] == [
        "delete_false_feedback"
    ]
    assert report["candidates"][0]["path_id"] == repair._opaque_path_id(
        vault,
        generated,
        "feedback",
    )


def test_audit_preserves_explicit_direct_user_feedback_even_with_idle_trigger(tmp_path):
    vault, state = _vault(tmp_path)
    direct = _write(
        vault / "knowledge" / "feedback" / "direct-user.json",
        json.dumps(_feedback_record(source_role="user")),
    )

    report = json.loads(_run("audit", vault, state).stdout)

    assert report["candidates"] == []
    preserved = [
        item for item in report["diagnostics"] if item["kind"] == "feedback_preserved"
    ]
    assert len(preserved) == 1
    assert preserved[0]["metadata"]["classification"] == "direct_user"
    assert direct.exists()


def test_audit_deletes_only_whole_generated_dailies_with_complete_coverage(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = vault / "knowledge" / "daily"
    empty = _write(daily / "2026-08-01.md", "")
    header = _write(daily / "2026-08-02.md", "# Daily 2026-08-02\n\n")
    generated = _write(
        daily / "2026-08-03.md",
        "# Daily 2026-08-03\n\n"
        "- `[10:00:00] tool | service-session | demo | Read` \n"
        "## [10:01:00] opencode-idle | service-session\n"
        "- Trigger: `opencode-idle`\n"
        "- Tier: `major`\n\n"
        "(no body)\n"
        "## [10:02:00] status | service-session\n(no body)\n"
        "## [10:03:00] shell | service-session\n(empty)\n"
        "## [10:04:00] service | service-session\n(no body)\n",
    )
    mixed_payloads = {
        "2026-08-04.md": generated.read_text(encoding="utf-8")
        + "A durable decision remains.\n",
        "2026-08-05.md": "# Daily 2026-08-05\n## [not-a-time] malformed\n",
        "2026-08-06.md": "# Daily 2026-08-06\nordinary unrecognized text\n",
        "2026-08-07.md": (
            "# Daily 2026-08-07\n"
            "## [10:00:00] opencode-idle | service-session\n"
            f"<!-- llm-wiki-capture: {'a' * 64} -->\n"
        ),
    }
    mixed = [_write(daily / name, payload) for name, payload in mixed_payloads.items()]
    before = {path: path.read_bytes() for path in mixed}

    report = json.loads(_run("audit", vault, state).stdout)
    targets = [
        item for item in report["candidates"] if item["action"] == "delete_generated_daily"
    ]

    assert {item["path_id"] for item in targets} == {
        repair._opaque_path_id(vault, path, "daily")
        for path in (empty, header, generated)
    }
    assert {
        item["path_id"]
        for item in report["diagnostics"]
        if item["kind"] == "daily_preserved"
    } == {repair._opaque_path_id(vault, path, "daily") for path in mixed}
    assert {path: path.read_bytes() for path in before} == before


def _render_writer_daily(tmp_path: Path, body: str) -> tuple[str, str]:
    from flush_memory import render_flush_block

    project_root = tmp_path / "project"
    project_root.mkdir(exist_ok=True)
    return render_flush_block(
        "minor",
        body,
        event="pre-compact",
        session_id="service-session",
        trigger="opencode-idle",
        project_slug="demo",
        project_root=str(project_root),
        occurred_at="2026-08-03T10:01:00",
    )


def test_audit_covers_exact_completion_marker_in_writer_generated_empty_daily(tmp_path):
    import repair_installed_memory as repair
    from daily_log_append import _append_unlocked
    from session_start_context import DAILY_RECORD_COMPLETION_MARKER

    vault, state = _vault(tmp_path)
    day, block = _render_writer_daily(tmp_path, "(no body)")
    daily = vault / "knowledge" / "daily" / f"{day}.md"
    _append_unlocked(daily, block)
    source = daily.read_text(encoding="utf-8")
    assert source.startswith(f"# Daily Session Memory \N{EM DASH} {day}\n")
    assert source.splitlines().count(DAILY_RECORD_COMPLETION_MARKER) == 1

    report = json.loads(_run("audit", vault, state).stdout)

    targets = [
        item for item in report["candidates"] if item["action"] == "delete_generated_daily"
    ]
    assert len(targets) == 1
    assert targets[0]["path_id"] == repair._opaque_path_id(vault, daily, "daily")
    assert targets[0]["metadata"]["generated_record_count"] == 1


@pytest.mark.parametrize("variant", ("malformed", "duplicate", "misplaced", "mixed"))
def test_audit_preserves_noncanonical_completion_marker_daily(
    tmp_path,
    variant,
):
    import repair_installed_memory as repair
    from daily_log_append import _append_unlocked
    from session_start_context import DAILY_RECORD_COMPLETION_MARKER

    vault, state = _vault(tmp_path)
    body = "**Lessons / patterns**\n- Durable user-authored information." if variant == "mixed" else "(no body)"
    day, block = _render_writer_daily(tmp_path, body)
    if variant == "malformed":
        block = block.replace(
            DAILY_RECORD_COMPLETION_MARKER,
            "<!-- llm-wiki-record-complete -- >",
        )
    elif variant == "duplicate":
        block += DAILY_RECORD_COMPLETION_MARKER + "\n"
    elif variant == "misplaced":
        block = DAILY_RECORD_COMPLETION_MARKER + "\n" + block
    daily = vault / "knowledge" / "daily" / f"{day}.md"
    _append_unlocked(daily, block)
    before = daily.read_bytes()

    report = json.loads(_run("audit", vault, state).stdout)

    assert report["candidates"] == []
    preserved = [
        item for item in report["diagnostics"] if item["kind"] == "daily_preserved"
    ]
    assert [item["path_id"] for item in preserved] == [
        repair._opaque_path_id(vault, daily, "daily")
    ]
    assert daily.read_bytes() == before


def test_audit_targets_only_visible_placeholder_in_trusted_project_state(tmp_path):
    import repair_installed_memory as repair
    from session_start_project_state import STATE_SECTION_TEMPLATE_PLACEHOLDERS

    vault, state = _vault(tmp_path)
    projects = vault / "knowledge" / "projects"
    placeholder = STATE_SECTION_TEMPLATE_PLACEHOLDERS["where we left off"]

    def state_file(slug: str, handoff: str) -> Path:
        project_root = tmp_path / "worktrees" / slug
        project_root.mkdir(parents=True)
        return _write(projects / slug / "state.md", _project_state(slug, project_root, handoff))

    trusted = state_file("trusted", placeholder)
    hidden_comment = state_file("comment", f"<!-- {placeholder} -->")
    hidden_fence = state_file("fence", f"```text\n{placeholder}\n```")
    hidden_html = state_file("html", f"<div>\n{placeholder}\n</div>")
    real = state_file("real", "- Continue the verified transaction work.")
    template_root = tmp_path / "worktrees" / "template"
    template_root.mkdir(parents=True)
    template = _write(
        projects / "_template" / "state.md",
        _project_state("_template", template_root, placeholder),
    )
    malformed = _write(
        projects / "malformed" / "state.md",
        "# malformed\n## Where we left off\n" + placeholder + "\n",
    )
    paths = (trusted, hidden_comment, hidden_fence, hidden_html, real, template, malformed)
    before = {path: path.read_bytes() for path in paths}

    report = json.loads(_run("audit", vault, state).stdout)
    targets = [
        item for item in report["candidates"] if item["action"] == "mark_handoff_unavailable"
    ]

    assert len(targets) == 1
    assert targets[0]["path_id"] == repair._opaque_path_id(
        vault, trusted, "project-state"
    )
    assert {path: path.read_bytes() for path in before} == before


def test_audit_rejects_out_of_scope_session_inventory_without_reading(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    sessions = _write(
        tmp_path / "terminal-tasks.json",
        '[{"title":"memory-private-terminal","orphaned":true}]',
    )
    real_read_text = Path.read_text

    def reject_read(path, *args, **kwargs):
        if path == sessions:
            raise AssertionError("out-of-scope terminal inventory was read")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_read)

    with pytest.raises(repair.RepairError, match="out of scope"):
        repair.inventory(vault, state, sessions)

    assert sessions.exists()
    assert not state.exists()


def test_audit_rejects_symlinked_source_without_following(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    vault, state = _vault(tmp_path)
    outside = _write(tmp_path / "outside.md", "do not inspect\n")
    link = vault / "knowledge" / "daily" / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    result = _run("audit", vault, state, check=False)

    assert result.returncode != 0
    assert "unsafe" in result.stderr.casefold() or "inventory" in result.stderr.casefold()
    assert outside.read_bytes() == b"do not inspect\n"
    assert not state.exists()


def test_audit_fails_closed_at_inventory_and_file_byte_bounds(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    first = _write(
        vault / "knowledge" / "daily" / "2026-08-02.md",
        "# Daily 2026-08-02\n",
    )
    second = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    before = {path: path.read_bytes() for path in (first, second)}
    monkeypatch.setattr(repair, "MAX_REPAIR_INVENTORY_ENTRIES", 1)
    with pytest.raises(repair.RepairError, match="inventory"):
        repair.inventory(vault, state)

    monkeypatch.setattr(repair, "MAX_REPAIR_INVENTORY_ENTRIES", 10_000)
    monkeypatch.setattr(repair, "MAX_REPAIR_DAILY_BYTES", 8)
    with pytest.raises(repair.RepairError, match="oversized"):
        repair.inventory(vault, state)
    assert {path: path.read_bytes() for path in before} == before
    assert not state.exists()


def test_audit_rejects_hardlinked_action_zone(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    source = _write(
        vault / "knowledge" / "daily" / "2026-08-02.md",
        "# Daily 2026-08-02\n",
    )
    hardlink = vault / "knowledge" / "daily" / "2026-08-03.md"
    try:
        os.link(source, hardlink)
    except OSError:
        pytest.skip("hard links unavailable")

    with pytest.raises(repair.RepairError, match="unsafe"):
        repair.inventory(vault, state)
    assert source.read_bytes() == hardlink.read_bytes()


def test_audit_ignores_special_file_without_opening(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation unavailable")
    vault, state = _vault(tmp_path)
    fifo = vault / "knowledge" / "daily" / "special.md"
    try:
        os.mkfifo(fifo)
    except OSError:
        pytest.skip("FIFO creation unavailable")

    report = json.loads(_run("audit", vault, state).stdout)

    assert report["candidates"] == []
    assert fifo.exists()
    assert not state.exists()


def test_backup_only_prepares_unapproved_sealed_v4_staging_for_every_action(tmp_path):
    data = _actionable_fixture(tmp_path)
    vault = data["vault"]
    state = data["state"]
    stale_relative = data["stale_relative"]
    source_before = {
        path: path.read_bytes()
        for path in (
            data["shadow"],
            data["stale"],
            data["generated_feedback"],
            data["generated_daily"],
            data["project_state"],
        )
    }

    audit, manifest_path, prepared = _prepare(vault, state, stale_relative)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    seal = json.loads(
        manifest_path.with_name("manifest.seal.json").read_text(encoding="utf-8")
    )

    assert prepared["status"] == "staging_prepared"
    assert manifest["schema_version"] == 4
    assert manifest["status"] == "prepared"
    assert manifest["approved"] is False
    assert manifest["root_fingerprint"]
    assert manifest["audit_report_sha256"] == _sha(audit.read_bytes())
    assert len(manifest["files"]) == 5
    assert {entry["action"] for entry in manifest["files"]} == {
        "delete_exact_duplicate_note",
        "delete_stale_note",
        "delete_false_feedback",
        "delete_generated_daily",
        "mark_handoff_unavailable",
    }
    for entry in manifest["files"]:
        source = vault / entry["path"]
        staged = manifest_path.parent / entry["source_staging_path"]
        assert staged.read_bytes() == source.read_bytes()
        assert entry["before_sha256"] == entry["staged_sha256"] == _sha(staged.read_bytes())
        assert entry["before_size"] == entry["staged_size"] == staged.stat().st_size
        assert entry["before_identity"]["nlink"] == 1
        expected_postcondition = (
            {"kind": "sha256", "sha256": entry["after_sha256"]}
            if entry["action"] == "mark_handoff_unavailable"
            else {"kind": "absent"}
        )
        assert entry["postcondition"] == expected_postcondition
    assert seal == {
        "schema_version": 4,
        "sealed_manifest_sha256": _sha(
            json.dumps(
                {key: value for key, value in manifest.items() if key != "approved"},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        ),
    }
    assert {path: path.read_bytes() for path in source_before} == source_before


def test_v4_preparation_journal_owns_staging_before_first_source_write(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit = _audit_file(vault, state)

    transaction_dir = _crash_v4_preparation(
        vault,
        state,
        audit,
        after_first_staging_write=False,
    )

    journal = json.loads((transaction_dir / "transaction.json").read_text(encoding="utf-8"))
    assert journal["schema_version"] == 4
    assert journal["status"] == "preparing"
    assert journal["staged_path_ids"] == []
    assert journal["staging_files"] == [
        {
            "path_id": journal["staging_files"][0]["path_id"],
            "source_staging_path": (
                f"source-staging/{journal['staging_files'][0]['path_id']}.source"
            ),
            "staged_sha256": _sha(daily.read_bytes()),
            "staged_size": len(daily.read_bytes()),
        }
    ]
    assert list((transaction_dir / "source-staging").iterdir()) == []


@pytest.mark.parametrize("unknown_artifact", (False, True))
def test_v4_preparation_recovery_purges_only_owned_exact_staging(
    tmp_path,
    unknown_artifact,
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit = _audit_file(vault, state)
    transaction_dir = _crash_v4_preparation(
        vault,
        state,
        audit,
        after_first_staging_write=True,
    )
    unknown = transaction_dir / "source-staging" / "unknown.source"
    if unknown_artifact:
        unknown.write_bytes(b"foreign source bytes")

    if unknown_artifact:
        with pytest.raises(repair.RepairError, match="inventory|unknown"):
            repair.recover_incomplete_transactions(vault, state)
        assert unknown.read_bytes() == b"foreign source bytes"
    else:
        repair.recover_incomplete_transactions(vault, state)
        journal = json.loads(
            (transaction_dir / "transaction.json").read_text(encoding="utf-8")
        )
        assert journal["status"] == "preparation_aborted"
        assert not (transaction_dir / "source-staging").exists()
    assert daily.read_bytes() == b"# Daily 2026-08-03\n"


def test_v4_preparation_recovery_rejects_missing_unpurged_staged_artifact(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    for day in (2, 3):
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
    _audit, manifest_path, _prepared = _prepare(vault, state)
    journal_path = manifest_path.with_name("transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["status"] = "preparing"
    journal_path.write_bytes(repair._json_bytes(journal))
    missing_id = journal["staged_path_ids"][-1]
    missing = manifest_path.parent / f"source-staging/{missing_id}.source"
    missing.unlink()
    survivors = {
        path: path.read_bytes()
        for path in (manifest_path.parent / "source-staging").iterdir()
    }
    journal_before = journal_path.read_bytes()

    with pytest.raises(repair.TransactionError, match="staged|staging|missing|purge"):
        repair.recover_incomplete_transactions(vault, state)

    assert journal_path.read_bytes() == journal_before
    assert {path: path.read_bytes() for path in survivors} == survivors
    assert not missing.exists()


def test_v4_prepared_journal_is_reused_by_approved_apply(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit, manifest, _prepared = _prepare(vault, state)
    prepared = json.loads(
        manifest.with_name("transaction.json").read_text(encoding="utf-8")
    )

    assert prepared["status"] == "prepared"
    assert prepared["staged_path_ids"] == [prepared["staging_files"][0]["path_id"]]
    created_at = prepared["created_at"]
    _approve_manifest(manifest)

    result = _apply_manifest(vault, state, audit, manifest)

    assert json.loads(result.stdout)["status"] == "applied"
    committed = json.loads(
        manifest.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert committed["status"] == "committed"
    assert committed["created_at"] == created_at
    assert not daily.exists()


def test_v4_preparation_journal_binds_approval_normalized_manifest(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    _write(vault / "knowledge" / "daily" / "2026-08-03.md", "# Daily 2026-08-03\n")
    _audit, manifest_path, _prepared = _prepare(vault, state)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    journal = json.loads(
        manifest_path.with_name("transaction.json").read_text(encoding="utf-8")
    )
    expected = _sha(repair._manifest_sealed_bytes(manifest))

    assert journal.get("prepared_manifest_sha256") == expected
    manifest["approved"] = True
    assert _sha(repair._manifest_sealed_bytes(manifest)) == expected


def test_apply_rejects_resealed_identity_rewrite_after_same_byte_replacement(tmp_path):
    import repair_installed_memory as repair
    from vault_editorial import read_bounded_note_snapshot

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    original = daily.read_bytes()
    audit, manifest_path, _prepared = _prepare(vault, state)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_identity = manifest["files"][0]["before_identity"]
    replacement = daily.with_suffix(".replacement")
    replacement.write_bytes(original)
    os.replace(replacement, daily)
    replacement_identity = repair._identity_record(
        read_bounded_note_snapshot(daily).file_identity
    )
    assert replacement_identity != original_identity
    manifest["approved"] = True
    manifest["files"][0]["before_identity"] = replacement_identity
    _write_manifest(manifest_path, manifest, reseal=True)

    result = _apply_manifest(vault, state, audit, manifest_path, check=False)

    assert result.returncode != 0
    assert "manifest" in result.stderr.casefold() and "binding" in result.stderr.casefold()
    assert daily.read_bytes() == original
    journal = json.loads(
        manifest_path.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "prepared"
    assert journal["mutated_path_ids"] == []


def test_prepared_recovery_rejects_resealed_manifest_identity_rewrite(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    _audit, manifest_path, _prepared = _prepare(vault, state)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["before_identity"]["mtime_ns"] += 1
    _write_manifest(manifest_path, manifest, reseal=True)

    with pytest.raises(repair.RepairError, match="manifest.*binding"):
        repair.recover_incomplete_transactions(vault, state)
    assert daily.exists()


def test_verify_rejects_resealed_manifest_identity_rewrite_after_commit(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit, manifest_path, _prepared = _prepare(vault, state)
    _approve_manifest(manifest_path)
    _apply_manifest(vault, state, audit, manifest_path)
    assert not daily.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["before_identity"]["mtime_ns"] += 1
    _write_manifest(manifest_path, manifest, reseal=True)

    with pytest.raises(repair.RepairError, match="manifest.*binding"):
        repair.verify_repair(vault, state, manifest_path)


def test_v4_recovery_rejects_type_confused_journal_progress_cleanly(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    _audit, manifest, _prepared = _prepare(vault, state)
    journal_path = manifest.with_name("transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["purged_path_ids"] = [{}]
    journal_path.write_bytes(repair._json_bytes(journal))

    with pytest.raises(repair.RepairError, match="purged_path_ids|purge progress"):
        repair.recover_incomplete_transactions(vault, state)
    assert daily.exists()


def test_v4_transaction_status_invariants_reject_contradictory_progress(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    for day in (2, 3):
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
    _audit, manifest_path, _prepared = _prepare(vault, state)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prepared = json.loads(
        manifest_path.with_name("transaction.json").read_text(encoding="utf-8")
    )
    entries = sorted(manifest["files"], key=lambda entry: entry["path"])
    action_ids = [entry["path_id"] for entry in entries]
    staging_ids = [
        item["path_id"]
        for item in sorted(
            prepared["staging_files"],
            key=lambda item: item["source_staging_path"],
        )
    ]
    results = [
        {
            "path_id": entry["path_id"],
            "action": entry["action"],
            "after_sha256": entry["after_sha256"],
            "postcondition": entry["postcondition"]["kind"],
        }
        for entry in entries
    ]
    fake_id = next(value * 64 for value in "fedcba" if value * 64 not in action_ids)

    def journal_for(status):
        journal = json.loads(json.dumps(prepared))
        journal["status"] = status
        if status == "preparation_aborted":
            journal["purged_path_ids"] = staging_ids
        elif status == "committing":
            journal["attempted_path_ids"] = action_ids
            journal["mutated_path_ids"] = action_ids[:-1]
            journal["results"] = results[:-1]
        elif status == "aborted_precondition":
            journal["commit_error"] = "PreMutationError: injected drift"
        elif status in {
            "rolling_back",
            "rollback_complete_purge_pending",
            "rolled_back",
            "critical_rollback_failed",
        }:
            journal["attempted_path_ids"] = action_ids
            journal["mutated_path_ids"] = action_ids
            journal["restored_path_ids"] = list(reversed(action_ids))
            journal["results"] = results
            journal["commit_error"] = "OSError: injected failure"
            if status == "rolled_back":
                journal["purged_path_ids"] = staging_ids
        elif status in {"committed_pending_purge", "committed"}:
            journal["attempted_path_ids"] = action_ids
            journal["mutated_path_ids"] = action_ids
            journal["results"] = results
            if status == "committed":
                journal["purged_path_ids"] = staging_ids
        elif status == "critical_manual_recovery":
            issue = {"path_id": action_ids[-1], "error": "OSError: rollback failed"}
            journal["attempted_path_ids"] = action_ids
            journal["mutated_path_ids"] = action_ids
            journal["restored_path_ids"] = action_ids[-2::-1]
            journal["results"] = results
            journal["commit_error"] = "OSError: injected failure"
            journal["rollback_errors"] = [issue]
            journal["manual_recovery"] = [issue]
        return journal

    def changed(status, key, value):
        journal = journal_for(status)
        journal[key] = value
        return journal

    fake_result = {**results[-1], "path_id": fake_id}
    fake_issue = {"path_id": fake_id, "error": "OSError: rollback failed"}
    for status in (
        "preparing",
        "preparation_purge_pending",
        "preparation_aborted",
        "prepared",
        "aborted_precondition",
        "committing",
        "rolling_back",
        "rollback_complete_purge_pending",
        "rolled_back",
        "committed_pending_purge",
        "committed",
        "critical_manual_recovery",
        "critical_rollback_failed",
    ):
        repair._validate_v4_transaction_journal(
            journal_for(status),
            vault,
            state,
            manifest,
        )

    rolling_omission = changed(
        "rolling_back",
        "restored_path_ids",
        list(reversed(action_ids))[1:],
    )
    rolling_overlap = journal_for("rolling_back")
    overlap_issue = {"path_id": action_ids[-1], "error": "OSError: rollback failed"}
    rolling_overlap["rollback_errors"] = [overlap_issue]
    rolling_overlap["manual_recovery"] = [overlap_issue]
    critical_omission = changed("critical_manual_recovery", "restored_path_ids", [])
    critical_overlap = changed(
        "critical_manual_recovery",
        "restored_path_ids",
        list(reversed(action_ids)),
    )
    nonmutated_restore = journal_for("rolling_back")
    nonmutated_restore["mutated_path_ids"] = action_ids[:1]
    nonmutated_restore["results"] = results[:1]
    nonmutated_restore["restored_path_ids"] = action_ids[1:]
    first_issue = {"path_id": action_ids[0], "error": "OSError: rollback failed"}
    nonmutated_restore["rollback_errors"] = [first_issue]
    nonmutated_restore["manual_recovery"] = [first_issue]
    error_order = changed("critical_manual_recovery", "restored_path_ids", [])
    ordered_wrong = [
        {"path_id": path_id, "error": "OSError: rollback failed"}
        for path_id in action_ids
    ]
    error_order["rollback_errors"] = ordered_wrong
    error_order["manual_recovery"] = ordered_wrong
    contradictions = [
        ("committed missing attempts", changed("committed", "attempted_path_ids", [])),
        (
            "committed foreign attempt",
            changed("committed", "attempted_path_ids", [action_ids[0], fake_id]),
        ),
        (
            "committed duplicate attempt",
            changed("committed", "attempted_path_ids", [action_ids[0]] * 2),
        ),
        ("committed missing mutations", changed("committed", "mutated_path_ids", [])),
        (
            "committed foreign mutation",
            changed("committed", "mutated_path_ids", [action_ids[0], fake_id]),
        ),
        (
            "committed duplicate mutation",
            changed("committed", "mutated_path_ids", [action_ids[0]] * 2),
        ),
        ("committed missing results", changed("committed", "results", [])),
        (
            "committed foreign result",
            changed("committed", "results", [results[0], fake_result]),
        ),
        (
            "committed duplicate result",
            changed("committed", "results", [results[0], results[0]]),
        ),
        ("committed missing purge", changed("committed", "purged_path_ids", [])),
        (
            "committed foreign purge",
            changed("committed", "purged_path_ids", [staging_ids[0], fake_id]),
        ),
        (
            "committed duplicate purge",
            changed("committed", "purged_path_ids", [staging_ids[0]] * 2),
        ),
        ("prepared attempt", changed("prepared", "attempted_path_ids", action_ids[:1])),
        ("prepared mutation", changed("prepared", "mutated_path_ids", action_ids[:1])),
        ("prepared restore", changed("prepared", "restored_path_ids", action_ids[:1])),
        ("prepared result", changed("prepared", "results", results[:1])),
        ("prepared purge", changed("prepared", "purged_path_ids", staging_ids[:1])),
        ("prepared error", changed("prepared", "commit_error", "tampered")),
        (
            "preparation abort omits staged purge accounting",
            changed("preparation_aborted", "purged_path_ids", staging_ids[:-1]),
        ),
        (
            "non-prefix commit attempt",
            changed("committing", "attempted_path_ids", action_ids[1:]),
        ),
        (
            "commit mutation ahead of attempt",
            changed("committing", "attempted_path_ids", []),
        ),
        (
            "commit result lags mutation",
            changed("committing", "results", []),
        ),
        ("rolling back omits a mutated action", rolling_omission),
        ("rolling back overlaps restored and unresolved", rolling_overlap),
        ("manual recovery omits a mutated action", critical_omission),
        ("manual recovery overlaps restored and unresolved", critical_overlap),
        ("rollback accounts a nonmutated action", nonmutated_restore),
        ("rollback errors are not reverse ordered", error_order),
        (
            "critical rollback failure omits a mutated action",
            changed(
                "critical_rollback_failed",
                "restored_path_ids",
                list(reversed(action_ids))[1:],
            ),
        ),
        (
            "rollback restore is not reverse ordered",
            changed("rollback_complete_purge_pending", "restored_path_ids", action_ids),
        ),
        (
            "rollback omits a mutated action",
            changed(
                "rollback_complete_purge_pending",
                "restored_path_ids",
                list(reversed(action_ids))[1:],
            ),
        ),
        (
            "rollback purge is not a prefix",
            changed(
                "rollback_complete_purge_pending",
                "purged_path_ids",
                staging_ids[1:],
            ),
        ),
        (
            "rolled back omits a restored mutation",
            changed("rolled_back", "restored_path_ids", list(reversed(action_ids))[1:]),
        ),
        (
            "rolled back has a foreign restore",
            changed("rolled_back", "restored_path_ids", [action_ids[-1], fake_id]),
        ),
        (
            "rolled back has a duplicate restore",
            changed("rolled_back", "restored_path_ids", [action_ids[-1]] * 2),
        ),
        (
            "rolled back has incomplete purge",
            changed("rolled_back", "purged_path_ids", staging_ids[:-1]),
        ),
        (
            "rolled back has reversed purge",
            changed("rolled_back", "purged_path_ids", list(reversed(staging_ids))),
        ),
        (
            "manual recovery has foreign errors",
            changed("critical_manual_recovery", "rollback_errors", [fake_issue]),
        ),
        (
            "manual recovery error lists disagree",
            changed("critical_manual_recovery", "manual_recovery", []),
        ),
    ]

    accepted = []
    for name, journal in contradictions:
        try:
            repair._validate_v4_transaction_journal(journal, vault, state, manifest)
        except repair.TransactionError:
            continue
        accepted.append(name)

    assert accepted == []


def test_apply_rejects_prepared_journal_with_mutation_progress(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit, manifest_path, _prepared = _prepare(vault, state)
    _approve_manifest(manifest_path)
    journal_path = manifest_path.with_name("transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["attempted_path_ids"] = [journal["staged_path_ids"][0]]
    journal_path.write_bytes(repair._json_bytes(journal))

    result = _apply_manifest(vault, state, audit, manifest_path, check=False)

    assert result.returncode != 0
    assert "progress" in result.stderr.casefold() or "status" in result.stderr.casefold()
    assert daily.exists()


def test_recovery_rejects_committing_journal_with_nonprefix_attempt(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    dailies = [
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
        for day in (2, 3)
    ]
    _audit, manifest_path, _prepared = _prepare(vault, state)
    _approve_manifest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    action_ids = [
        entry["path_id"] for entry in sorted(manifest["files"], key=lambda entry: entry["path"])
    ]
    journal_path = manifest_path.with_name("transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["status"] = "committing"
    journal["attempted_path_ids"] = action_ids[1:]
    journal_path.write_bytes(repair._json_bytes(journal))

    with pytest.raises(repair.TransactionError, match="progress|status"):
        repair.recover_incomplete_transactions(vault, state)
    assert all(path.exists() for path in dailies)


def test_verify_rejects_committed_journal_with_incomplete_purge_progress(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit, manifest_path, _prepared = _prepare(vault, state)
    _approve_manifest(manifest_path)
    _apply_manifest(vault, state, audit, manifest_path)
    assert not daily.exists()
    journal_path = manifest_path.with_name("transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["purged_path_ids"] = []
    journal_path.write_bytes(repair._json_bytes(journal))

    with pytest.raises(repair.TransactionError, match="progress|status"):
        repair.verify_repair(vault, state, manifest_path)


def test_manifest_must_be_in_direct_backup_child(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    _write(vault / "knowledge" / "daily" / "2026-08-03.md", "# Daily 2026-08-03\n")
    _audit, manifest, _prepared = _prepare(vault, state)
    nested_root = manifest.parent.parent / "nested"
    nested_root.mkdir()
    nested_transaction = nested_root / manifest.parent.name
    manifest.parent.rename(nested_transaction)
    nested_manifest = nested_transaction / "manifest.json"

    with pytest.raises(repair.RepairError, match="direct|run/backups"):
        repair.validate_manifest(
            nested_manifest,
            vault,
            state,
            require_approved=False,
        )


@pytest.mark.parametrize(
    "tamper",
    (
        "unapproved",
        "approval-type",
        "duplicate-key",
        "unknown-action-resealed",
        "path-resealed",
        "extra-field-resealed",
        "stale-list-resealed",
        "identity-type-resealed",
        "audit-digest-resealed",
        "missing-seal",
        "seal-mismatch",
        "staged-bytes",
    ),
)
def test_apply_rejects_every_non_approval_manifest_change_before_mutation(
    tmp_path, tamper
):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    original = daily.read_bytes()
    audit, manifest_path, _prepared = _prepare(vault, state)
    prepared_journal = manifest_path.with_name("transaction.json").read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if tamper != "unapproved":
        manifest["approved"] = True

    if tamper == "approval-type":
        manifest["approved"] = 1
        _write_manifest(manifest_path, manifest)
    elif tamper == "duplicate-key":
        raw = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(
            raw.replace(
                '  "approved": false,',
                '  "approved": true,\n  "approved": true,',
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
    elif tamper == "unknown-action-resealed":
        manifest["candidates"][0]["action"] = "delete_arbitrary_path"
        manifest["files"][0]["action"] = "delete_arbitrary_path"
        _write_manifest(manifest_path, manifest, reseal=True)
    elif tamper == "path-resealed":
        manifest["files"][0]["path"] = "run/queue/private.json"
        _write_manifest(manifest_path, manifest, reseal=True)
    elif tamper == "extra-field-resealed":
        manifest["operator_note"] = "not part of the sealed schema"
        _write_manifest(manifest_path, manifest, reseal=True)
    elif tamper == "stale-list-resealed":
        manifest["stale_pages"] = ["knowledge/notes/not-reviewed.md"]
        _write_manifest(manifest_path, manifest, reseal=True)
    elif tamper == "identity-type-resealed":
        manifest["files"][0]["before_identity"]["size"] = True
        _write_manifest(manifest_path, manifest, reseal=True)
    elif tamper == "audit-digest-resealed":
        manifest["audit_report_sha256"] = "0" * 64
        _write_manifest(manifest_path, manifest, reseal=True)
    elif tamper == "missing-seal":
        _write_manifest(manifest_path, manifest)
        seal = manifest_path.with_name("manifest.seal.json")
        seal.chmod(0o600)
        seal.unlink()
    elif tamper == "seal-mismatch":
        _write_manifest(manifest_path, manifest)
        seal = manifest_path.with_name("manifest.seal.json")
        seal.chmod(0o600)
        seal.write_text('{"schema_version":4,"sealed_manifest_sha256":"' + "0" * 64 + '"}')
    elif tamper == "staged-bytes":
        _write_manifest(manifest_path, manifest)
        entry = manifest["files"][0]
        (manifest_path.parent / entry["source_staging_path"]).write_bytes(b"tampered")
    elif tamper != "unapproved":
        _write_manifest(manifest_path, manifest)

    result = _apply_manifest(vault, state, audit, manifest_path, check=False)

    assert result.returncode != 0
    assert daily.read_bytes() == original
    assert manifest_path.with_name("transaction.json").read_bytes() == prepared_journal


def test_apply_rejects_duplicate_audit_json_key_before_staging(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit = _audit_file(vault, state)
    raw = audit.read_text(encoding="utf-8")
    audit.write_text(
        raw.replace('  "mode": "audit",', '  "mode": "audit",\n  "mode": "audit",', 1),
        encoding="utf-8",
        newline="\n",
    )

    result = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit),
        "--backup-only",
        check=False,
    )

    assert result.returncode != 0
    assert "duplicate" in result.stderr.casefold()
    assert daily.exists()
    assert not state.exists()


def test_apply_rejects_stale_cli_audit_and_source_drift_before_mutation(tmp_path):
    vault, state = _vault(tmp_path)
    stale_relative = "knowledge/notes/stale.md"
    stale = _write(vault / stale_relative, "# Explicit stale\n\nBody.\n")
    audit, manifest, _prepared = _prepare(vault, state, stale_relative)
    prepared_journal = manifest.with_name("transaction.json").read_bytes()
    _approve_manifest(manifest)

    missing_cli = _apply_manifest(vault, state, audit, manifest, check=False)
    assert missing_cli.returncode != 0
    assert "stale" in missing_cli.stderr.casefold()
    assert stale.exists()
    assert manifest.with_name("transaction.json").read_bytes() == prepared_journal

    audit.write_bytes(audit.read_bytes() + b" ")
    changed_audit = _apply_manifest(
        vault,
        state,
        audit,
        manifest,
        stale_relative,
        check=False,
    )
    assert changed_audit.returncode != 0
    assert stale.exists()


def test_apply_rejects_same_byte_source_identity_swap_before_first_mutation(tmp_path):
    vault, state = _vault(tmp_path)
    first = _write(
        vault / "knowledge" / "daily" / "2026-08-02.md",
        "# Daily 2026-08-02\n",
    )
    second = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    originals = {path: path.read_bytes() for path in (first, second)}
    audit, manifest, _prepared = _prepare(vault, state)
    _approve_manifest(manifest)
    replacement = second.with_suffix(".replacement")
    replacement.write_bytes(originals[second])
    os.replace(replacement, second)

    result = _apply_manifest(vault, state, audit, manifest, check=False)

    assert result.returncode != 0
    assert {path: path.read_bytes() for path in originals} == originals
    transaction = json.loads(
        manifest.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["status"] == "aborted_precondition"
    assert transaction["mutated_path_ids"] == []


def test_per_file_cas_rejects_late_same_byte_swap_and_rolls_back(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    first = _write(
        vault / "knowledge" / "daily" / "2026-08-02.md",
        "# Daily 2026-08-02\n",
    )
    second = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    originals = {path: path.read_bytes() for path in (first, second)}
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest)
    real_commit = repair._v4_commit_entry
    calls = 0

    def swap_second(entry, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            replacement = second.with_suffix(".replacement")
            replacement.write_bytes(originals[second])
            os.replace(replacement, second)
        return real_commit(entry, *args, **kwargs)

    monkeypatch.setattr(repair, "_v4_commit_entry", swap_second)

    with pytest.raises(repair.TransactionError, match="rolled back"):
        repair.apply_repair(
            report,
            audit_bytes,
            vault,
            state,
            manifest,
            backup_only=False,
        )

    assert {path: path.read_bytes() for path in originals} == originals
    assert not (manifest.parent / "source-staging").exists()


def test_v4_full_action_workflow_purges_staging_and_preserves_sentinels(tmp_path):
    data = _actionable_fixture(tmp_path)
    vault = data["vault"]
    state = data["state"]
    stale_relative = data["stale_relative"]
    sentinel_paths = [
        state / "run" / "queue" / "pending.json",
        state / "run" / "queue" / "failed" / "terminal.json",
        state / "run" / "compile-journal" / "journal.json",
        state / "run" / "compile-manifests" / "manifest.json",
        state / "run" / "compile-receipts" / "receipt.json",
    ]
    sentinels = {}
    for index, path in enumerate(sentinel_paths):
        _write(path, json.dumps({"sentinel": index, "private": True}))
        sentinels[path] = path.read_bytes()

    audit, manifest, _prepared = _prepare(vault, state, stale_relative)
    manifest_data = _approve_manifest(manifest)
    source_bytes = {
        (vault / entry["path"]).read_bytes() for entry in manifest_data["files"]
    }
    applied = json.loads(
        _apply_manifest(vault, state, audit, manifest, stale_relative).stdout
    )

    assert applied["status"] == "applied"
    assert all(
        not path.exists()
        for path in (
            data["shadow"],
            data["stale"],
            data["generated_feedback"],
            data["generated_daily"],
        )
    )
    assert {path: path.read_bytes() for path in data["retained"]} == data["retained"]
    assert data["project_state"].read_bytes() == data["project_before"].replace(
        data["placeholder"].encode(),
        b"(saved project handoff unavailable)",
    )
    assert not (manifest.parent / "source-staging").exists()
    assert not (manifest.parent / "files").exists()
    assert not (manifest.parent / "quarantine").exists()
    assert all(
        path.read_bytes() not in source_bytes
        for path in manifest.parent.rglob("*")
        if path.is_file()
    )
    assert {path: path.read_bytes() for path in sentinels} == sentinels

    before_vault = _snapshot(vault)
    before_state = _snapshot(state)
    first = _run("verify", vault, state, "--manifest", str(manifest))
    second = _run("verify", vault, state, "--manifest", str(manifest))
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["status"] == "verified"
    assert _snapshot(vault) == before_vault
    assert _snapshot(state) == before_state
    assert {path: path.read_bytes() for path in sentinels} == sentinels


def test_v4_partial_failure_rolls_back_prior_deletion_and_purges_staging(
    tmp_path, monkeypatch
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    files = [
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
        for day in (2, 3)
    ]
    originals = {path: path.read_bytes() for path in files}
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest)
    real_commit = repair._v4_commit_entry
    calls = 0

    def fail_second(entry, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-action failure")
        return real_commit(entry, *args, **kwargs)

    monkeypatch.setattr(repair, "_v4_commit_entry", fail_second)

    with pytest.raises(repair.TransactionError, match="rolled back"):
        repair.apply_repair(
            report,
            audit_bytes,
            vault,
            state,
            manifest,
            backup_only=False,
        )

    assert {path: path.read_bytes() for path in originals} == originals
    assert not (manifest.parent / "source-staging").exists()
    transaction = json.loads(
        manifest.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["status"] == "rolled_back"
    assert len(transaction["restored_path_ids"]) == 1
    assert transaction["rollback_errors"] == []


def test_v4_rollback_records_mutated_action_already_restored_before_retry(
    tmp_path,
    monkeypatch,
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    files = [
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
        for day in (2, 3)
    ]
    originals = {path: path.read_bytes() for path in files}
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest)
    real_commit = repair._v4_commit_entry
    first_entry = None

    def restore_first_before_second_failure(entry, *args, **kwargs):
        nonlocal first_entry
        if first_entry is None:
            first_entry = entry
            return real_commit(entry, *args, **kwargs)
        first_source = vault / first_entry["path"]
        first_source.write_bytes(
            (manifest.parent / first_entry["source_staging_path"]).read_bytes()
        )
        raise OSError("injected failure after external restoration")

    monkeypatch.setattr(repair, "_v4_commit_entry", restore_first_before_second_failure)

    with pytest.raises(repair.TransactionError, match="rolled back"):
        repair.apply_repair(
            report,
            audit_bytes,
            vault,
            state,
            manifest,
            backup_only=False,
        )

    journal = json.loads(
        manifest.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "rolled_back"
    assert set(journal["mutated_path_ids"]).issubset(journal["restored_path_ids"])
    repair.recover_incomplete_transactions(vault, state)
    assert {path: path.read_bytes() for path in originals} == originals


def test_v4_rollback_postcondition_failure_retains_staging_as_critical(
    tmp_path,
    monkeypatch,
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    for day in (2, 3):
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest_path)
    real_commit = repair._v4_commit_entry
    calls = 0

    def fail_second(entry, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-action failure")
        return real_commit(entry, *args, **kwargs)

    monkeypatch.setattr(repair, "_v4_commit_entry", fail_second)
    monkeypatch.setattr(repair, "_v4_rollback_postconditions", lambda *_args: False)

    with pytest.raises(repair.TransactionError, match="roll.*back|recovery"):
        repair.apply_repair(
            report,
            audit_bytes,
            vault,
            state,
            manifest_path,
            backup_only=False,
        )

    journal = json.loads(
        manifest_path.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "critical_rollback_failed"
    assert (manifest_path.parent / "source-staging").is_dir()
    assert set(journal["mutated_path_ids"]) == (
        set(journal["restored_path_ids"])
        | {issue["path_id"] for issue in journal["rollback_errors"]}
    )


def test_v4_recovery_postcondition_failure_retains_staging_as_critical(
    tmp_path,
    monkeypatch,
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit = _audit_file(vault, state)
    manifest_path = _crash_v4_after_first_action(vault, state, audit)
    monkeypatch.setattr(repair, "_v4_rollback_postconditions", lambda *_args: False)

    with pytest.raises(repair.TransactionError, match="roll.*back|recovery"):
        repair.recover_incomplete_transactions(vault, state)

    journal = json.loads(
        manifest_path.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "critical_rollback_failed"
    assert (manifest_path.parent / "source-staging").is_dir()
    assert set(journal["mutated_path_ids"]) == set(journal["restored_path_ids"])
    assert journal["rollback_errors"] == []
    assert daily.exists()


def test_v4_rollback_persists_completion_before_staging_purge(tmp_path, monkeypatch):
    vault, _state, manifest, originals = _interrupt_v4_rollback_before_purge(
        tmp_path,
        monkeypatch,
    )

    journal = json.loads(manifest.with_name("transaction.json").read_text(encoding="utf-8"))
    assert {path: path.read_bytes() for path in originals} == originals
    assert journal["status"] == "rollback_complete_purge_pending"
    assert journal["purged_path_ids"] == []
    assert (manifest.parent / "source-staging").is_dir()


@pytest.mark.parametrize("recorded", (True, False))
def test_v4_rollback_recovery_accepts_only_recorded_missing_staging(
    tmp_path,
    monkeypatch,
    recorded,
):
    import repair_installed_memory as repair

    vault, state, manifest, originals = _interrupt_v4_rollback_before_purge(
        tmp_path,
        monkeypatch,
    )
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    journal_path = manifest.with_name("transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    first_purge_id = sorted(
        journal["staging_files"],
        key=lambda item: item["source_staging_path"],
    )[0]["path_id"]
    first = next(
        entry for entry in manifest_data["files"] if entry["path_id"] == first_purge_id
    )
    journal["status"] = "rollback_complete_purge_pending"
    journal["purged_path_ids"] = [first["path_id"]] if recorded else []
    journal_path.write_bytes(repair._json_bytes(journal))
    staged = manifest.parent / first["source_staging_path"]
    staged.unlink()

    if recorded:
        repair.recover_incomplete_transactions(vault, state)
        completed = json.loads(journal_path.read_text(encoding="utf-8"))
        assert completed["status"] == "rolled_back"
        assert not (manifest.parent / "source-staging").exists()
    else:
        with pytest.raises(repair.RepairError, match="staging.*missing"):
            repair.recover_incomplete_transactions(vault, state)
        assert (manifest.parent / "source-staging").is_dir()
    assert {path: path.read_bytes() for path in originals} == originals


def test_v4_partial_failure_rolls_back_project_edit_byte_exactly(tmp_path, monkeypatch):
    import repair_installed_memory as repair
    from session_start_project_state import STATE_SECTION_TEMPLATE_PLACEHOLDERS

    vault, state = _vault(tmp_path)
    placeholder = STATE_SECTION_TEMPLATE_PLACEHOLDERS["where we left off"]
    states = []
    for slug in ("alpha", "beta"):
        project_root = tmp_path / "worktrees" / slug
        project_root.mkdir(parents=True)
        states.append(
            _write(
                vault / "knowledge" / "projects" / slug / "state.md",
                _project_state(slug, project_root, placeholder),
            )
        )
    originals = {path: path.read_bytes() for path in states}
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest)
    real_commit = repair._v4_commit_entry
    calls = 0

    def fail_second(entry, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second project edit failure")
        return real_commit(entry, *args, **kwargs)

    monkeypatch.setattr(repair, "_v4_commit_entry", fail_second)

    with pytest.raises(repair.TransactionError, match="rolled back"):
        repair.apply_repair(
            report,
            audit_bytes,
            vault,
            state,
            manifest,
            backup_only=False,
        )

    assert {path: path.read_bytes() for path in states} == originals
    assert not (manifest.parent / "source-staging").exists()


def test_next_prepare_recovers_interrupted_v4_and_purges_old_staging(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    original = daily.read_bytes()
    audit = _audit_file(vault, state)
    interrupted = _crash_v4_after_first_action(vault, state, audit)
    assert not daily.exists()

    next_prepared = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit),
        "--backup-only",
    )

    assert json.loads(next_prepared.stdout)["status"] == "staging_prepared"
    assert daily.read_bytes() == original
    journal = json.loads(
        interrupted.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "rolled_back"
    assert not (interrupted.parent / "source-staging").exists()


def test_verify_finishes_crashed_committed_v4_staging_purge(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit, manifest, _prepared = _prepare(vault, state)
    _approve_manifest(manifest)
    code = f"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import repair_installed_memory as repair
vault = Path({str(vault)!r})
state = Path({str(state)!r})
audit = Path({str(audit)!r})
audit_bytes = audit.read_bytes()
report = json.loads(audit_bytes)
manifest = Path({str(manifest)!r})
def crash_purge(*_args, **_kwargs):
    os._exit(92)
repair._purge_v4_source_staging = crash_purge
repair.apply_repair(report, audit_bytes, vault, state, manifest, backup_only=False)
"""
    crashed = subprocess.run([sys.executable, "-c", code], cwd=ROOT)
    assert crashed.returncode == 92
    assert not daily.exists()
    assert (manifest.parent / "source-staging").is_dir()

    verified = json.loads(
        _run("verify", vault, state, "--manifest", str(manifest)).stdout
    )

    assert verified["status"] == "verified"
    assert not (manifest.parent / "source-staging").exists()
    journal = json.loads(manifest.with_name("transaction.json").read_text(encoding="utf-8"))
    assert journal["status"] == "committed"


def test_verify_finishes_interrupted_partial_v4_staging_purge(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    dailies = [
        _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
        for day in (2, 3)
    ]
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest)
    real_purge = repair._purge_v4_source_staging

    def interrupt_partial_purge(_manifest, manifest_path, outcome):
        staging_root = manifest_path.parent / "source-staging"
        artifact = sorted(staging_root.iterdir())[0]
        entry = next(
            item
            for item in _manifest["files"]
            if (manifest_path.parent / item["source_staging_path"]) == artifact
        )
        outcome["purged_path_ids"].append(entry["path_id"])
        repair._persist_transaction(manifest_path, outcome)
        artifact.unlink()
        repair._fsync_directory(staging_root)
        raise OSError("injected partial staging purge failure")

    monkeypatch.setattr(repair, "_purge_v4_source_staging", interrupt_partial_purge)
    with pytest.raises(repair.TransactionError):
        repair.apply_repair(
            report,
            audit_bytes,
            vault,
            state,
            manifest,
            backup_only=False,
        )

    assert all(not path.exists() for path in dailies)
    staging_root = manifest.parent / "source-staging"
    assert len(list(staging_root.iterdir())) == 1
    journal = json.loads(manifest.with_name("transaction.json").read_text(encoding="utf-8"))
    assert journal["status"] == "committed_pending_purge"

    monkeypatch.setattr(repair, "_purge_v4_source_staging", real_purge)
    verified = repair.verify_repair(vault, state, manifest)

    assert verified["status"] == "verified"
    assert not staging_root.exists()
    journal = json.loads(manifest.with_name("transaction.json").read_text(encoding="utf-8"))
    assert journal["status"] == "committed"


def test_v4_recovery_preserves_recreated_source_for_manual_recovery(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit = _audit_file(vault, state)
    interrupted = _crash_v4_after_first_action(vault, state, audit)
    recreated = b"# Recreated durable daily\nDo not overwrite this content.\n"
    daily.write_bytes(recreated)

    with pytest.raises(repair.TransactionError, match="manual recovery"):
        repair.recover_incomplete_transactions(vault, state)

    assert daily.read_bytes() == recreated
    assert (interrupted.parent / "source-staging").is_dir()
    journal = json.loads(
        interrupted.with_name("transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "critical_manual_recovery"
    assert journal["manual_recovery"][0]["current_sha256"] == _sha(recreated)


def test_v4_retry_clears_persisted_rollback_error_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    originals = {}
    for day in (2, 3):
        source = _write(
            vault / "knowledge" / "daily" / f"2026-08-0{day}.md",
            f"# Daily 2026-08-0{day}\n",
        )
        originals[source] = source.read_bytes()
    _audit, manifest_path, _prepared = _prepare(vault, state)
    _approve_manifest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = sorted(manifest["files"], key=lambda entry: entry["path"])
    action_ids = [entry["path_id"] for entry in entries]
    for entry in entries:
        (vault / entry["path"]).unlink()
    restored_entry, unresolved_entry = entries
    restored_source = vault / restored_entry["path"]
    restored_source.write_bytes(
        (manifest_path.parent / restored_entry["source_staging_path"]).read_bytes()
    )
    issue = {
        "path_id": unresolved_entry["path_id"],
        "action": unresolved_entry["action"],
        "reason": "OSError: persisted rollback failure",
        "staged_sha256": unresolved_entry["staged_sha256"],
        "current_sha256": None,
    }
    journal_path = manifest_path.with_name("transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.update(
        {
            "status": "critical_manual_recovery",
            "attempted_path_ids": action_ids,
            "mutated_path_ids": action_ids,
            "restored_path_ids": [restored_entry["path_id"]],
            "results": [
                {
                    "path_id": entry["path_id"],
                    "action": entry["action"],
                    "after_sha256": entry["after_sha256"],
                    "postcondition": entry["postcondition"]["kind"],
                }
                for entry in entries
            ],
            "commit_error": "OSError: injected commit failure",
            "rollback_errors": [issue],
            "manual_recovery": [issue],
        }
    )
    journal_path.write_bytes(repair._json_bytes(journal))
    events = []
    real_validate = repair._validate_v4_transaction_journal
    real_purge = repair._purge_v4_source_staging

    def record_validation(outcome, *args, **kwargs):
        if outcome.get("status") == "rollback_complete_purge_pending":
            events.append("validate-before-purge")
        return real_validate(outcome, *args, **kwargs)

    def record_purge(*args, **kwargs):
        events.append("purge")
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(repair, "_validate_v4_transaction_journal", record_validation)
    monkeypatch.setattr(repair, "_purge_v4_source_staging", record_purge)

    repair.recover_incomplete_transactions(vault, state)

    completed = json.loads(journal_path.read_text(encoding="utf-8"))
    assert completed["status"] == "rolled_back"
    assert completed["restored_path_ids"] == list(reversed(action_ids))
    assert completed["rollback_errors"] == []
    assert completed["manual_recovery"] == []
    assert events == ["validate-before-purge", "purge"]
    assert {path: path.read_bytes() for path in originals} == originals
    terminal_journal = journal_path.read_bytes()

    repair.recover_incomplete_transactions(vault, state)

    assert journal_path.read_bytes() == terminal_journal
    assert {path: path.read_bytes() for path in originals} == originals


def test_interrupted_v3_recovers_without_purging_legacy_artifacts(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    source = _write(
        vault / "knowledge" / "daily" / "2026-07-26.md",
        "# Daily\n- `[10:00:00] tool | service | demo | Read` \n",
    )
    original = source.read_bytes()
    staged = b"# Daily\n"
    source.write_bytes(staged)
    manifest = _interrupted_v3_transaction(vault, state, source, original, staged)
    artifacts = {
        path: path.read_bytes()
        for path in manifest.parent.rglob("*")
        if path.is_file() and path.name != "transaction.json"
    }

    repair.recover_incomplete_transactions(vault, state)

    assert source.read_bytes() == original
    assert {path: path.read_bytes() for path in artifacts} == artifacts
    journal = json.loads(manifest.with_name("transaction.json").read_text(encoding="utf-8"))
    assert journal["status"] == "rolled_back"
    assert (manifest.parent / "files").is_dir()
    assert (manifest.parent / "staged").is_dir()


def test_verify_rejects_unsafe_output_before_recovery(tmp_path):
    vault, state = _vault(tmp_path)
    source = _write(
        vault / "knowledge" / "daily" / "2026-07-26.md",
        "# Daily\n- `[10:00:00] tool | service | demo | Read` \n",
    )
    original = source.read_bytes()
    staged = b"# Daily\n"
    source.write_bytes(staged)
    manifest = _interrupted_v3_transaction(vault, state, source, original, staged)

    result = _run(
        "verify",
        vault,
        state,
        "--manifest",
        str(manifest),
        "--output",
        str(vault / "verify.json"),
        check=False,
    )

    assert result.returncode != 0
    assert source.read_bytes() == staged
    journal = json.loads(manifest.with_name("transaction.json").read_text(encoding="utf-8"))
    assert journal["status"] == "committing"


def test_mutating_apply_rejects_schema_v3_manifest(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit = _audit_file(vault, state)
    report = json.loads(audit.read_bytes())
    manifest = _write_v3_report_manifest(
        vault,
        state,
        report["candidates"],
        audit_digest=_sha(audit.read_bytes()),
        diagnostics=report["diagnostics"],
        stale_pages=report["stale_pages"],
    )

    result = _apply_manifest(vault, state, audit, manifest, check=False)

    assert result.returncode != 0
    assert "schema v4" in result.stderr.casefold()
    assert daily.exists()
    assert not manifest.with_name("transaction.json").exists()


@pytest.mark.parametrize(
    ("action", "status"),
    (
        ("erase_unreviewed_content", "candidate"),
        ("propose_safe_api_delete", "candidate"),
    ),
)
def test_v3_verify_rejects_unknown_or_unhandled_candidate_action(
    tmp_path,
    action,
    status,
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    candidate = {
        "id": "legacy-report-only",
        "kind": "orphan_service_session",
        "path_id": "b" * 64,
        "action": action,
        "before_sha256": "c" * 64,
        "after_sha256": "c" * 64,
        "reason": "legacy v3 report-only fixture",
        "status": status,
        "metadata": {"title_prefix": "memory-", "orphan_evidence": True},
    }
    manifest = _write_v3_report_manifest(vault, state, [candidate])

    with pytest.raises(repair.RepairError, match="action|candidate|status"):
        repair.verify_repair(vault, state, manifest)


def test_verify_requires_committed_v4_transaction(tmp_path):
    vault, state = _vault(tmp_path)
    _write(vault / "knowledge" / "daily" / "2026-08-03.md", "# Daily 2026-08-03\n")
    _audit, manifest, _prepared = _prepare(vault, state)

    result = _run(
        "verify",
        vault,
        state,
        "--manifest",
        str(manifest),
        check=False,
    )

    assert result.returncode != 0
    assert "committed" in result.stderr.casefold() or "approved" in result.stderr.casefold()


def test_verify_rejects_recreated_deleted_target_without_changing_it(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit, manifest, _prepared = _prepare(vault, state)
    _approve_manifest(manifest)
    _apply_manifest(vault, state, audit, manifest)
    recreated = b"# Recreated durable daily\n"
    daily.write_bytes(recreated)

    result = _run(
        "verify",
        vault,
        state,
        "--manifest",
        str(manifest),
        check=False,
    )

    assert result.returncode != 0
    assert daily.read_bytes() == recreated


def test_two_approved_v4_appliers_share_one_committed_result(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-08-03.md",
        "# Daily 2026-08-03\n",
    )
    audit = _audit_file(vault, state)
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    manifest = repair.create_backup(report, audit_bytes, vault, state)
    _approve_manifest(manifest)
    barrier = threading.Barrier(2)
    errors = []

    def apply_worker():
        barrier.wait()
        try:
            repair.apply_repair(
                report,
                audit_bytes,
                vault,
                state,
                manifest,
                backup_only=False,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=apply_worker) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    assert all(not worker.is_alive() for worker in workers)
    assert not daily.exists()
    journal = json.loads(manifest.with_name("transaction.json").read_text(encoding="utf-8"))
    assert journal["status"] == "committed"
    assert len(journal["results"]) == 1


def test_repair_lock_order_is_repair_publication_daily_feedback_project(
    tmp_path, monkeypatch
):
    import daily_log_append
    import feedback_capture
    import memory_state
    import repair_installed_memory as repair

    state = tmp_path / "state"
    events = []

    @contextmanager
    def tracked(name):
        events.append(f"enter:{name}")
        try:
            yield
        finally:
            events.append(f"exit:{name}")

    def advisory(path, **_kwargs):
        return tracked(
            {
                "repair-recovery.lock": "repair",
                "knowledge-publication.lock": "publication",
                "project-state-claim.lock": "project",
            }[path.name]
        )

    monkeypatch.setattr(memory_state, "advisory_file_lock", advisory)
    monkeypatch.setattr(
        daily_log_append,
        "_daily_lock",
        lambda **_kwargs: tracked("daily"),
    )
    monkeypatch.setattr(
        feedback_capture,
        "feedback_writer_lock",
        lambda *_args, **_kwargs: tracked("feedback"),
    )

    with repair._repair_writer_locks(state):
        events.append("body")

    assert events == [
        "enter:repair",
        "enter:publication",
        "enter:daily",
        "enter:feedback",
        "enter:project",
        "body",
        "exit:project",
        "exit:feedback",
        "exit:daily",
        "exit:publication",
        "exit:repair",
    ]
