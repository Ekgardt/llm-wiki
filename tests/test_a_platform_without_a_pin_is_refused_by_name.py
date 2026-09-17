"""Audit 3, B22: an unpinned platform is refused before anything is downloaded.

Research: `docs/research/2026-09-17-inst-a-platform-without-a-pin-is-refused-by-name.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import install_language_server as installer  # noqa: E402
import lsp_paths  # noqa: E402
import lsp_profiles  # noqa: E402


def _on_an_unpinned_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(installer.platform, "machine", lambda: "riscv64")


@pytest.mark.parametrize("name", ["gopls", "rust-analyzer"])
def test_an_unpinned_platform_is_named_and_nothing_is_fetched(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_an_unpinned_platform(monkeypatch)
    profile = lsp_profiles.REGISTRY.get(name)

    with pytest.raises(installer.InstallError) as error:
        installer.install_language_server(profile, state_root=tmp_path)

    text = str(error.value)
    assert ("no pinned artifact for this platform" in text, "FreeBSD riscv64" in text) == (
        True,
        True,
    )
    assert not (tmp_path / "cache").exists()


def test_a_profile_pinned_once_for_every_platform_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_an_unpinned_platform(monkeypatch)

    installer._require_pinned_platform(lsp_profiles.REGISTRY.get("typescript"))


def test_the_pyright_release_is_typed_once() -> None:
    source = (SCRIPTS_DIR / "lsp_profiles.py").read_bytes()

    assert lsp_profiles.PYRIGHT_VERSION is lsp_paths.PYRIGHT_VERSION
    assert lsp_paths.PYRIGHT_VERSION.encode() not in source


def test_the_go_release_is_typed_once() -> None:
    source = (SCRIPTS_DIR / "lsp_profiles.py").read_bytes()

    assert b"dl.google.com/go/go1" not in source
    assert all(
        f"/go{lsp_profiles.GO_VERSION}." in artifact.url
        for artifact in lsp_profiles.GO_ARTIFACTS
    )
