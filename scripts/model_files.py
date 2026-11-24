"""Where a pinned model's files live: ordinary files under `cache/models/<repo>/<revision>/`.

OpenVINO refuses an ONNX model whose external weights are reached through a link
outside the model's directory, and the Hugging Face cache stores every file as a
link into a shared blob store. So the installer writes real files here, one
directory per pinned revision, and the read path opens nothing else.
"""
from __future__ import annotations

from pathlib import Path


def models_root() -> Path:
    from memory_state import STATE_ROOT

    return STATE_ROOT / "cache" / "models"


def model_directory(repo_id: str, revision: str) -> Path:
    return models_root() / repo_id.replace("/", "--") / revision


def installed_file(repo_id: str, revision: str, name: str) -> Path:
    """One file of the pinned revision; `FileNotFoundError` when it is not installed."""
    path = model_directory(repo_id, revision) / name
    if not path.is_file():
        raise FileNotFoundError(f"{repo_id}@{revision[:12]}: {name} is not installed")
    return path
