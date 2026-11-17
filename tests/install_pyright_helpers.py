"""Helpers shared by test_install_pyright.py and the test files importing them."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyright_profile import PYRIGHT_PACKAGE_URL

from tests.pyright_install_artifacts import (
    PyrightInstallArtifactFixture,
    create_pyright_install_artifact,
    use_pyright_install_artifact_identity,
)


def _artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: object,
) -> PyrightInstallArtifactFixture:
    artifact = create_pyright_install_artifact(tmp_path / "pyright.tgz", **kwargs)
    use_pyright_install_artifact_identity(monkeypatch, artifact)
    return artifact


def _root(state_root: Path) -> Path:
    return state_root / "cache/code-tools/pyright/1.1.411"


class _ResponseSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)


class _Response(io.BytesIO):
    def __init__(
        self,
        content: bytes,
        *,
        url: str = PYRIGHT_PACKAGE_URL,
        status: int = 200,
    ) -> None:
        super().__init__(content)
        self.status = status
        self._url = url
        self.headers: dict[str, str] = {"Content-Length": str(len(content))}
        self.socket = _ResponseSocket()
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
