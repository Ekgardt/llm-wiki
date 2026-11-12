"""Helpers shared by test_install_models.py and the test files importing them."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import ModuleType

import install_models


class _FakeHub(ModuleType):
    """A Hub whose cache and downloads are directories under tmp_path."""

    def __init__(self, root: Path, contents: dict[str, bytes]) -> None:
        super().__init__("huggingface_hub")
        self.root = root
        self.contents = contents
        self.downloads: list[tuple[str, str, list[str]]] = []

    def _snapshot(self, repo_id: str, revision: str) -> Path:
        return self.root / repo_id.replace("/", "--") / "snapshots" / revision

    def try_to_load_from_cache(self, repo_id, filename, *, revision):
        path = self._snapshot(repo_id, revision) / filename
        if path.is_file():
            return str(path)
        return None

    def snapshot_download(self, repo_id, *, revision, allow_patterns):
        self.downloads.append((repo_id, revision, list(allow_patterns)))
        snapshot = self._snapshot(repo_id, revision)
        for name in allow_patterns:
            (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
            (snapshot / name).write_bytes(self.contents[repo_id] if name == WEIGHTS else b"companion")
        return str(snapshot)


WEIGHTS = "onnx/model.onnx"


COMPANION = "tokenizer.json"


def _pinned_to(contents: dict[str, bytes]) -> tuple[install_models.PinnedModel, ...]:
    return tuple(
        install_models.PinnedModel(
            repo_id,
            "f" * 40,
            WEIGHTS,
            hashlib.sha256(blob).hexdigest(),
            len(blob),
            (WEIGHTS, COMPANION),
            ("model.safetensors",),
        )
        for repo_id, blob in contents.items()
    )
