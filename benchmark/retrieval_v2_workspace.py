"""Semantic artifact cleanup, lexical fallback reports, model prefetch and per-run workspaces."""
from __future__ import annotations

import os
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import retrieval_v2_outputs
import retrieval_v2_transformers
from retrieval_v2_contract import MODEL_MATRIX_ADAPTER_KIND, ModelSelection
from retrieval_v2_environment import environment_provenance, runner_sha256, sha256_json
from retrieval_v2_matrix import matrix_target
from retrieval_v2_outputs import (
    is_reparse_point,
    model_cache_environment,
    prepared_model_cache_root,
    validate_cache_root,
)
from retrieval_v2_transformers import load_transformer_reranker


def _remove_reparse_point(path: Path) -> None:
    if path.is_dir():
        path.rmdir()
        return
    path.unlink(missing_ok=True)


def _remove_artifact(path: Path) -> None:
    if is_reparse_point(path):
        _remove_reparse_point(path)
        return
    _remove_plain_artifact(path)


def _remove_plain_artifact(path: Path) -> None:
    if path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)


def remove_semantic_artifacts(cache_root: Path, lexical_path: Path) -> None:
    for path in cache_root.iterdir():
        if path == lexical_path:
            continue
        _remove_artifact(path)


def decorate_lexical_fallback(
    report: dict,
    *,
    selection: ModelSelection,
    lexical_config: str,
    vector_backend: str,
    acquisition_mode: str | None,
    reason: str,
) -> dict:
    report["adapter_kind"] = MODEL_MATRIX_ADAPTER_KIND
    report["quality_claim"] = False
    report["release_evidence"] = False
    report["requested_mode"] = MODEL_MATRIX_ADAPTER_KIND
    report["effective_mode"] = f"lexical-{lexical_config}"
    report["fallback_reason"] = reason
    report["model_id"] = selection.embedding["id"]
    report["variant_id"] = selection.variant["variant_id"]
    report["revision"] = selection.embedding["revision"]
    report["matrix_sha256"] = selection.matrix_sha256
    report["corpus_sha256"] = selection.corpus_sha256
    report["acquisition_mode"] = acquisition_mode
    report["vector_backend"] = vector_backend
    report["reranker"] = None
    report["benchmark_contract_sha256"] = sha256_json(selection.matrix["benchmark_contract"])
    report["benchmark_runner_sha256"] = runner_sha256()
    report["candidate"] = {
        "embedding": matrix_target(selection.embedding, selection.variant),
        "reranker": (
            matrix_target(selection.reranker, selection.reranker_variant)
            if selection.reranker is not None
            else None
        ),
    }
    report["gates"]["degraded"] = True
    report["gates"]["release_evidence"] = False
    report["gates"]["interpretation"] = "lexical-fallback"
    report["methodology"]["requested_retrieval_order"] = (
        "independent BM25 and dense generation with optional all-depth reranking"
    )
    return report


def prefetch_models(selection: ModelSelection, *, cache_root: Path | str) -> dict:
    cache_root = validate_cache_root(Path(cache_root))
    with model_cache_environment(cache_root, allow_download=True) as acquisition_mode:
        retrieval_v2_transformers.load_transformer_embedding(
            selection,
            cache_root=cache_root.resolve(),
            local_files_only=False,
            trust_remote_code=False,
        )
        if selection.reranker is not None:
            load_transformer_reranker(
                selection,
                cache_root=cache_root.resolve(),
                local_files_only=False,
                trust_remote_code=False,
            )
    return {
        "schema_version": 1,
        "artifact_kind": "model-acquisition-receipt",
        "quality_claim": False,
        "release_evidence": False,
        "acquisition_mode": acquisition_mode,
        "candidate": {
            "embedding": matrix_target(selection.embedding, selection.variant),
            "reranker": (
                matrix_target(selection.reranker, selection.reranker_variant)
                if selection.reranker is not None
                else None
            ),
        },
        "matrix_sha256": selection.matrix_sha256,
        "corpus_sha256": selection.corpus_sha256,
        "environment_provenance": environment_provenance(None),
    }


@dataclass(frozen=True)
class _RunWorkspace:
    path: Path
    identity: os.stat_result
    runs_root: Path
    runs_identity: os.stat_result
    model_cache_identity: str


def _require_owner_only(identity: os.stat_result, label: str) -> None:
    if hasattr(os, "getuid") and identity.st_uid != os.getuid():
        raise PermissionError(f"{label} is not owned by the current user")
    if os.name == "posix" and stat.S_IMODE(identity.st_mode) & 0o077:
        raise PermissionError(f"{label} must be owner-controlled")


def _owner_only_directory(path: Path, label: str) -> os.stat_result:
    if is_reparse_point(path):
        raise ValueError(f"{label} must not be a symlink or reparse point")
    identity = path.stat(follow_symlinks=False)
    _require_owner_only(identity, label)
    return identity


def _owned_root_identity(root: Path) -> os.stat_result:
    root_identity = root.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and root_identity.st_uid != os.getuid():
        raise PermissionError("model cache root is not owned by the current user")
    return root_identity


def _prepared_runs_root(root: Path) -> tuple[Path, os.stat_result]:
    runs_root = root / "runs"
    if runs_root.exists() and is_reparse_point(runs_root):
        raise ValueError("runs directory must not be a symlink or reparse point")
    runs_root.mkdir(mode=0o700, exist_ok=True)
    return runs_root, _owner_only_directory(runs_root, "runs directory")


def _allocated_workspace(runs_root: Path) -> Path:
    for _attempt in range(32):
        candidate = runs_root / secrets.token_hex(16)
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not allocate a unique benchmark run workspace")


def create_run_workspace(model_cache_root: Path) -> _RunWorkspace:
    root = prepared_model_cache_root(model_cache_root)
    root_identity = _owned_root_identity(root)
    runs_root, runs_identity = _prepared_runs_root(root)
    workspace = _allocated_workspace(runs_root)
    workspace_identity = _owner_only_directory(workspace, "run workspace")
    if not retrieval_v2_outputs.same_path_identity(runs_root, runs_identity):
        shutil.rmtree(workspace)
        raise PermissionError("runs directory changed during workspace creation")
    identity_payload = {"device": int(root_identity.st_dev), "inode": int(root_identity.st_ino)}
    return _RunWorkspace(workspace, workspace_identity, runs_root, runs_identity, sha256_json(identity_payload))


def _require_workspace_unchanged(workspace: _RunWorkspace) -> None:
    if not retrieval_v2_outputs.same_path_identity(workspace.runs_root, workspace.runs_identity):
        raise PermissionError("runs directory changed before workspace cleanup")
    if is_reparse_point(workspace.path) or not retrieval_v2_outputs.same_path_identity(workspace.path, workspace.identity):
        raise PermissionError("run workspace changed before cleanup")


def cleanup_run_workspace(workspace: _RunWorkspace) -> None:
    _require_workspace_unchanged(workspace)
    if os.name == "posix" and not shutil.rmtree.avoids_symlink_attacks:
        raise PermissionError("platform lacks descriptor-safe workspace cleanup")
    shutil.rmtree(workspace.path)
