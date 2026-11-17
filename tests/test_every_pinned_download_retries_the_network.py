"""A TCP reset during the Pyright download is tried again, from the start (audit 2026-09-27 C-10).

docs/research/2026-09-27-every-pinned-download-retries-the-network.md
"""
from __future__ import annotations

import ast
from pathlib import Path

import install_pyright as installer_module
from install_pyright import install_pyright

from tests.install_pyright_helpers import _artifact, _Response
from tests.script_sources import script_files, script_key

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class _ResetMidway(_Response):
    """Delivers the first chunk, then the connection is reset."""

    def read1(self, size: int = -1) -> bytes:
        if self.tell() > 0:
            raise ConnectionResetError(104, "Connection reset by peer")
        return super().read1(size)


def test_a_reset_download_is_fetched_again_and_the_install_succeeds(tmp_path, monkeypatch) -> None:
    content = _artifact(tmp_path, monkeypatch).path.read_bytes()
    responses = [_ResetMidway(content), _Response(content)]
    monkeypatch.setattr(installer_module, "COPY_CHUNK_BYTES", 16)
    monkeypatch.setattr(installer_module, "_open_pinned_url", lambda *args, **kwargs: responses.pop(0))

    installed = install_pyright(state_root=tmp_path / "state")

    assert (responses, installed.version) == ([], "1.1.411")


def _opens_a_pinned_url(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) in {"_open_pinned_url", "open_pinned_url", "download_pinned"}


def _called_without_retry(path: Path) -> bool:
    """A module that opens a pinned URL and never names `retry_transient`."""
    source = path.read_text(encoding="utf-8")
    opens = any(_opens_a_pinned_url(node) for node in ast.walk(ast.parse(source)))
    return opens and "retry_transient" not in source


def test_every_module_that_downloads_a_pin_retries_the_network() -> None:
    modules = [path for path in script_files() if path.name != "pinned_download.py"]

    assert [script_key(path) for path in modules if _called_without_retry(path)] == []
