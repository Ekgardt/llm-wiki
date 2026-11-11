"""Helpers shared by test_doctor.py and the test files importing them."""
from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


GENEROUS_BUDGET_SECONDS = 120.0


def _adopt(root: Path, state_root: Path) -> None:
    """Adopt Reliability V3 the way an install does, so the capture check can run.

    A vault that has not adopted reports capture as disabled (issue #17); a test
    that asserts a healthy report needs the real records under `run/`, not a
    patched answer.
    """
    from installed_memory_repair import repair_installed_vault

    (root / "scripts" / "integration_adapter.py").write_bytes(
        (SCRIPTS / "integration_adapter.py").read_bytes()
    )
    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    assert report["overall_status"] == "ok", report


def _codex_hooks_fixture() -> dict:
    command = {
        "type": "command",
        "command": 'uv run --directory "$LLM_WIKI_ROOT" python "$LLM_WIKI_ROOT/scripts/codex_memory.py" hook',
        "commandWindows": 'uv run --directory "%LLM_WIKI_ROOT%" python "%LLM_WIKI_ROOT%\\scripts\\codex_memory.py" hook',
        "timeout": 15,
    }
    return {
        "hooks": {
            "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [command]}],
            "PreCompact": [{"matcher": "manual|auto", "hooks": [command]}],
            "PostCompact": [{"matcher": "manual|auto", "hooks": [command]}],
            "Stop": [{"hooks": [command]}],
        }
    }


def _create_claim_index(root: Path, state_root: Path) -> None:
    from claims import ClaimIndex

    ClaimIndex(state_root, vault=root).rebuild([root / "knowledge" / "notes"])


def _create_generation(root: Path, state_root: Path) -> None:
    import corpus_snapshot
    import generation_maintenance
    from evidence_graph_builder import build_full_generation
    from llm_wiki.generation_catalog_store import GenerationCatalog
    from repository_scope import resolve_repository_scope

    # A test vault owns its Git boundary and must not inherit an ancestor's
    # possibly inaccessible repository marker from the execution environment.
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
    snapshot = corpus_snapshot.collect_corpus(root)
    build_full_generation(
        GenerationCatalog(state_root),
        sources=(
            {
                "source_id": source.record.logical_id,
                "relative_path": source.record.relative_path,
                "sha256": source.record.sha256,
                "size": source.record.size,
                "media_type": source.record.media_type,
                "language": source.record.language,
                "git_oid": source.record.git_oid,
            }
            for source in snapshot.sources
        ),
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="healthy-generation",
        graph_extractor_version=generation_maintenance.maintenance_extractor_identity(),
        repository_scope=resolve_repository_scope(root),
        snapshot=snapshot,
        publication_root=root,
    )


def _build_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    home = tmp_path / "home"
    for path in (
        root / "knowledge" / "notes",
        root / "scripts",
        root / "integrations" / "claude-code",
        root / "integrations" / "codex",
        state_root / "run",
        state_root / "logs",
        state_root / "cache",
        home,
    ):
        path.mkdir(parents=True, exist_ok=True)
    for relative in (
        "scripts/scheduled_nightly.py",
        "scripts/search_memory.py",
        "scripts/mcp_server.py",
        "scripts/llm-wiki-memory-opencode.js",
        "scripts/codex_memory.py",
        "integrations/claude-code/settings.json",
    ):
        (root / relative).write_text("{}\n", encoding="utf-8")
    (root / "integrations" / "codex" / "hooks.json").write_text(
        json.dumps(_codex_hooks_fixture(), indent=2) + "\n", encoding="utf-8"
    )
    return root, state_root, home


def _check(report: dict, check_id: str) -> dict:
    return next(item for item in report["checks"] if item["id"] == check_id)


def _cache_pinned_models(tmp_path: Path, monkeypatch) -> None:
    """The pinned model files present in a Hub cache of the test's own, as an install leaves them.

    The report used to read this machine's real cache, so a healthy-vault test
    passed or failed with whatever the machine had downloaded. See
    `docs/research/2026-09-25-the-encoder-runs-without-torch.md`.
    """
    import install_models

    hub = install_models.hub_library()
    if hub is None:
        return
    cache = tmp_path / "hub-cache"
    # The package loads `constants` lazily; import it by name to set the cache.
    constants = importlib.import_module(f"{hub.__name__}.constants")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    for model in install_models.pinned_models():
        _write_pinned_files(cache, model)


def _write_pinned_files(cache: Path, model) -> None:
    snapshot = cache / f"models--{model.repo_id.replace('/', '--')}" / "snapshots" / model.revision
    named = [pattern for pattern in model.allow_patterns if "*" not in pattern]
    for name in {model.weights_file, *named}:
        (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
        (snapshot / name).write_bytes(b"pinned")


def _qualified_pyright_check(*_args, **_kwargs) -> dict:
    return {
        "id": "pyright",
        "status": "ok",
        "message": "Pyright identity is qualified.",
        "details": {
            "status": "qualified",
            "qualified": True,
            "codes": [],
        },
    }


def _lsp_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_lsp_owner(
    state_root: Path,
    *,
    owner_nonce: str,
    generation_nonce: str,
    started_at: datetime,
    owner_pid: int,
) -> Path:
    owner = state_root / "run" / "lsp" / owner_nonce
    owner.mkdir(parents=True)
    (owner / "cancellation").mkdir()
    (owner / "owner.json").write_text(
        json.dumps(
            {
                "command_basename": "pyright-langserver",
                "generation_nonce": generation_nonce,
                "owner_nonce": owner_nonce,
                "owner_pid": owner_pid,
                "started_at": _lsp_timestamp(started_at),
                "state": "process_running",
            }
        ),
        encoding="utf-8",
    )
    return owner


def _write_lsp_failure(
    owner: Path,
    *,
    owner_nonce: str,
    generation_nonce: str,
    timestamp: datetime,
    server_pid: int | None = None,
) -> None:
    record: dict[str, object] = {
        "code": "process_exited",
        "generation_nonce": generation_nonce,
        "owner_nonce": owner_nonce,
        "timestamp": _lsp_timestamp(timestamp),
    }
    if server_pid is not None:
        record["server_pid"] = server_pid
    (owner / "failure.json").write_text(json.dumps(record), encoding="utf-8")


def _nightly_state(state_root: Path, payload: dict) -> None:
    (state_root / "run").mkdir(parents=True, exist_ok=True)
    (state_root / "run" / "state.json").write_text(json.dumps(payload), encoding="utf-8")
