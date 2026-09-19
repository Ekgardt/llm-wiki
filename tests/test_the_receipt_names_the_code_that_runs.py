"""Audit 3, B20: the install receipt attests the code, not only the loader.

`package/langserver.index.js` is a 229-byte shim that `require`s `./dist/...`;
until now it was the only file the receipt named, so a truncated or edited
bundle passed as the pinned install. Research:
`docs/research/2026-09-17-inst-the-receipt-names-the-code-that-runs.md`.
"""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from install_pyright import PyrightInstallError, install_pyright  # noqa: E402
from pyright_profile import (  # noqa: E402
    PYRIGHT_SERVER_RELATIVE,
    PyrightCandidates,
    discover_pyright,
)
from reliable_memory import canonical_json_bytes  # noqa: E402
from repository_scope import resolve_repository_scope  # noqa: E402

from tests.code_kernel_helpers import (  # noqa: E402
    PyrightTarEntry,
    create_pyright_install_artifact,
    create_python_repository,
    pyright_executed_tree_sha256,
    use_pyright_install_artifact_identity,
)

SERVER = (
    b"global.__rootDirectory = __dirname + '/dist/';\n"
    b"require('./dist/pyright-langserver');\n"
)
BUNDLE_NAME = "pyright-langserver.js"
BUNDLE = b"// the megabytes the process actually runs\n"
BUNDLES = {BUNDLE_NAME: BUNDLE}
PACKAGE_JSON = canonical_json_bytes({"name": "pyright", "version": "1.1.411"})


def _artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tarball shaped like the real one: a shim plus a bundle under `dist`."""
    entries = (
        PyrightTarEntry("package", kind=tarfile.DIRTYPE),
        PyrightTarEntry("package/package.json", PACKAGE_JSON),
        PyrightTarEntry("package/langserver.index.js", SERVER),
        PyrightTarEntry("package/dist", kind=tarfile.DIRTYPE),
        PyrightTarEntry(f"package/dist/{BUNDLE_NAME}", BUNDLE),
    )
    artifact = create_pyright_install_artifact(
        tmp_path / "pyright.tgz", entries=entries, server_bytes=SERVER
    )
    use_pyright_install_artifact_identity(monkeypatch, artifact)
    return artifact


def _installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Install into a managed state root; return that root and the install root."""
    artifact = _artifact(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    return state_root, install_pyright(
        state_root=state_root, artifact=artifact.path
    ).root


def _bundle(root: Path) -> Path:
    return root / "package/dist" / BUNDLE_NAME


def _discovered(tmp_path: Path, state_root: Path, root: Path):
    repository = create_python_repository(tmp_path / "repository")
    return discover_pyright(
        resolve_repository_scope(repository),
        state_root=state_root,
        candidates=PyrightCandidates((), (root / PYRIGHT_SERVER_RELATIVE,), ()),
    )


def test_the_receipt_covers_the_bundles_the_shim_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state_root, root = _installed(tmp_path, monkeypatch)

    receipt = json.loads((root / "install-manifest.json").read_bytes())

    assert (
        receipt["executed_tree_sha256"],
        receipt["schema_version"],
    ) == (
        pyright_executed_tree_sha256(SERVER, BUNDLES),
        "pyright-install/v2",
    )
    assert receipt["executed_tree_sha256"] != pyright_executed_tree_sha256(SERVER, {})


@pytest.mark.parametrize(
    "replacement",
    [
        # Same length, so nothing but the content betrays it.
        b"// the megabytes someone else put there\n!",
        b"",
    ],
    ids=("rewritten", "truncated"),
)
def test_an_edited_bundle_is_named_at_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: bytes
) -> None:
    state_root, root = _installed(tmp_path, monkeypatch)
    _bundle(root).write_bytes(replacement)

    result = _discovered(tmp_path, state_root, root)

    assert "pyright_executed_tree_digest_mismatch" in result.degradation_codes
    assert result.qualified is False


def test_an_untouched_install_is_not_accused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, root = _installed(tmp_path, monkeypatch)

    result = _discovered(tmp_path, state_root, root)

    assert not [
        code for code in result.degradation_codes if "executed_tree" in code
    ]


def test_a_receipt_written_before_the_tree_digest_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, root = _installed(tmp_path, monkeypatch)
    receipt = json.loads((root / "install-manifest.json").read_bytes())
    receipt.pop("executed_tree_sha256")
    receipt["schema_version"] = "pyright-install/v1"
    (root / "install-manifest.json").write_bytes(canonical_json_bytes(receipt))

    result = _discovered(tmp_path, state_root, root)

    assert "pyright_manifest_predates_tree_digest" in result.degradation_codes
    assert result.qualified is False


def test_revalidating_a_published_tree_rehashes_the_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lost-race path accepts an existing install; it must weigh the code too."""
    state_root, root = _installed(tmp_path, monkeypatch)
    _bundle(root).write_bytes(b"// edited after publication ............\n!")

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(
            state_root=state_root, artifact=tmp_path / "must-not-be-read.tgz"
        )

    assert error.value.code == "pyright_existing_install_invalid"
