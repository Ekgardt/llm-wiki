from pathlib import Path

import pytest
from lsp_paths import (
    LSP_RELATIVE_ROOT,
    PYRIGHT_RELATIVE_ROOT,
    PYRIGHT_VERSION,
    lsp_owner_root,
    managed_pyright_root,
)


def test_lsp_path_constants_are_exact() -> None:
    assert PYRIGHT_VERSION == "1.1.411"
    assert PYRIGHT_RELATIVE_ROOT == Path("cache/code-tools/pyright/1.1.411")
    assert LSP_RELATIVE_ROOT == Path("run/lsp")


def test_managed_pyright_root_resolves_state_root_without_creating_paths(
    tmp_path: Path,
) -> None:
    assert managed_pyright_root(tmp_path) == (
        tmp_path / "cache/code-tools/pyright/1.1.411"
    )
    assert not (tmp_path / "cache").exists()


def test_lsp_owner_root_resolves_state_root_without_creating_paths(
    tmp_path: Path,
) -> None:
    owner_nonce = "a" * 32

    assert lsp_owner_root(tmp_path, owner_nonce) == tmp_path / "run/lsp" / owner_nonce
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "owner_nonce",
    ["", "A" * 32, "a/b", "..", "a" * 31, True, Path("a" * 32)],
)
def test_lsp_owner_root_rejects_invalid_owner_nonce(
    tmp_path: Path, owner_nonce: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        lsp_owner_root(tmp_path, owner_nonce)  # type: ignore[arg-type]


@pytest.mark.parametrize("state_root", ["state", True])
def test_lsp_path_helpers_require_path_state_root(state_root: object) -> None:
    with pytest.raises(TypeError):
        managed_pyright_root(state_root)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        lsp_owner_root(state_root, "a" * 32)  # type: ignore[arg-type]
