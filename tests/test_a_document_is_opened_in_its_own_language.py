"""Audit 3, B14: `didOpen` names the document's own language, not always Python.

Research: `docs/research/2026-09-17-a-document-is-opened-in-its-own-language.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lsp_profiles import (  # noqa: E402
    GOPLS_PROFILE,
    PYRIGHT_PROFILE,
    RUST_ANALYZER_PROFILE,
    TYPESCRIPT_PROFILE,
)
from lsp_security import RepositorySource  # noqa: E402
from pyright_profile import PyrightIdentity  # noqa: E402
from pyright_session import LanguageServerSession, OpenDocument  # noqa: E402
from repository_scope import resolve_repository_scope  # noqa: E402

CASES = [
    (PYRIGHT_PROFILE, "pkg/service.py", "python"),
    (PYRIGHT_PROFILE, "pkg/service.pyi", "python"),
    (TYPESCRIPT_PROFILE, "src/app.ts", "typescript"),
    (TYPESCRIPT_PROFILE, "src/view.tsx", "typescriptreact"),
    (TYPESCRIPT_PROFILE, "src/tool.mjs", "javascript"),
    (TYPESCRIPT_PROFILE, "src/view.JSX", "javascriptreact"),
    (GOPLS_PROFILE, "cmd/main.go", "go"),
    (RUST_ANALYZER_PROFILE, "src/lib.rs", "rust"),
]


def _never_started_identity() -> PyrightIdentity:
    return PyrightIdentity(
        status="missing",
        source=None,
        version=None,
        node_executable=None,
        node_version=None,
        node_major=None,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256="a" * 64,
        configuration_sha256="b" * 64,
        qualified=False,
        degradation_codes=("pyright_missing",),
    )


def _document(scope, repository: Path, relative: str) -> OpenDocument:
    absolute = repository / relative
    source = RepositorySource(
        scope.repository_id, scope.checkout_id, relative, absolute, absolute.as_uri()
    )
    return OpenDocument(source, b"", "0" * 64, 1)


@pytest.mark.parametrize(("profile", "relative", "expected"), CASES)
def test_did_open_names_the_language_of_the_file(tmp_path, profile, relative, expected):
    scope = resolve_repository_scope(tmp_path)
    session = LanguageServerSession(
        scope, _never_started_identity(), state_root=tmp_path, profile=profile
    )
    params = session._did_open_params(_document(scope, tmp_path, relative))
    assert params["textDocument"]["languageId"] == expected


def test_an_unknown_suffix_gets_the_profiles_first_language() -> None:
    found = (
        GOPLS_PROFILE.language_id_for(".mod"),
        TYPESCRIPT_PROFILE.language_id_for(".py"),
    )
    assert found == ("go", "typescript")
