"""The second language server is reachable from the working path, not just written.

`CODE-08` landed `lsp_profiles` and `lsp_server_profile` with no importer, and
its own commit message named that as the defect: "written, tested, never run on
the working path". These tests are about the wiring, so every one of them enters
through something a request actually calls -- the registry the MCP boundary asks,
the discovery the session manager asks, the key the manager stores under, the
readiness predicate a query is admitted through.

They deliberately do **not** start a language server. Live TypeScript evidence
belongs in `tests/test_typescript_navigation.py`, which skips without the managed
artifact and says so.
"""

from __future__ import annotations

import time
from pathlib import Path

import lsp_identity
import lsp_profiles
import mcp_server
import pytest
from lsp_profiles import PYRIGHT_PROFILE, TYPESCRIPT_PROFILE
from lsp_server_profile import READINESS_WORK_DONE_PROGRESS
from pyright_profile import (
    PYRIGHT_CONFIGURATION_SHA256,
    PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
    PyrightIdentity,
)
from pyright_session import LanguageServerSession, LanguageServerSessionManager
from repository_scope import resolve_repository_scope


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.ts").write_bytes(b"export const value = 1;\n")
    (root / "tsconfig.json").write_bytes(b"{}\n")
    return root


def _identity(codes: tuple[str, ...] = ()) -> PyrightIdentity:
    return PyrightIdentity(
        status="qualified" if not codes else "degraded",
        source="managed",
        version="6.0.0",
        node_executable=Path("/usr/bin/node"),
        node_version="v22.23.2",
        node_major=22,
        server_executable=Path("/managed/cli.mjs"),
        executable_sha256="a" * 64,
        package_sha256=None,
        initialization_options_sha256=(
            lsp_identity.profile_initialization_options_sha256(TYPESCRIPT_PROFILE)
        ),
        configuration_sha256="b" * 64,
        qualified=not codes,
        degradation_codes=codes,
    )


# -- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/main.ts", "typescript"),
        ("src/app.tsx", "typescript"),
        ("bundle.mjs", "typescript"),
        ("pkg/api.py", "pyright"),
        ("pkg/api.pyi", "pyright"),
    ],
)
def test_the_boundary_routes_a_file_to_the_server_that_owns_its_suffix(
    path: str, expected: str
) -> None:
    assert mcp_server._navigation_profile(path).name == expected


def test_an_unclaimed_suffix_keeps_the_behaviour_it_had_before_profiles() -> None:
    """Not an error and not a new refusal: the same session it always opened."""
    assert mcp_server._navigation_profile("README.md") is PYRIGHT_PROFILE
    assert mcp_server._navigation_profile("main.go") is PYRIGHT_PROFILE


def _claimed_suffixes() -> dict[str, str]:
    claimed: dict[str, str] = {}
    for name in lsp_profiles.REGISTRY.names():
        for suffix in lsp_profiles.REGISTRY.get(name).file_suffixes:
            assert suffix not in claimed
            claimed[suffix] = name
    return claimed


def test_no_two_profiles_claim_one_suffix() -> None:
    claimed = _claimed_suffixes()
    assert claimed[".ts"] == "typescript"
    assert claimed[".py"] == "pyright"


# -- absence ---------------------------------------------------------------


def test_a_server_that_is_not_installed_is_a_named_limit_not_a_failure(
    tmp_path: Path,
) -> None:
    """Installation is a separate operator action, so absence is a steady state."""
    scope = resolve_repository_scope(_repository(tmp_path))
    identity = lsp_identity.discover_managed_server(
        TYPESCRIPT_PROFILE, scope, state_root=tmp_path / "empty-state"
    )
    assert identity.status == "missing"
    assert identity.qualified is False
    assert identity.degradation_codes == ("typescript_missing",)
    assert identity.server_executable is None


def test_discovery_never_creates_anything(tmp_path: Path) -> None:
    scope = resolve_repository_scope(_repository(tmp_path))
    state_root = tmp_path / "untouched"
    lsp_identity.discover_managed_server(
        TYPESCRIPT_PROFILE, scope, state_root=state_root
    )
    assert not state_root.exists()


def test_a_missing_project_configuration_is_not_a_degradation(tmp_path: Path) -> None:
    """An absent tsconfig weakens resolution; it must not disqualify the session."""
    root = _repository(tmp_path)
    (root / "tsconfig.json").unlink()
    scope = resolve_repository_scope(root)
    digest = lsp_identity.repository_configuration_digest(TYPESCRIPT_PROFILE, scope)
    assert len(digest) == 64


# -- the receipt -----------------------------------------------------------


def _receipt() -> dict[str, str]:
    return lsp_identity.build_install_manifest(
        TYPESCRIPT_PROFILE, server_sha256="c" * 64, runtime_sha256="d" * 64
    )


def test_the_receipt_pins_both_artifacts() -> None:
    manifest = _receipt()
    assert manifest["package_url"] == lsp_profiles.TYPESCRIPT_PACKAGE_URL
    assert manifest["runtime_url"] == lsp_profiles.TSSERVER_PACKAGE_URL
    assert manifest["runtime_relative_path"] == "typescript/lib/tsserver.js"
    assert lsp_identity.validate_install_manifest(TYPESCRIPT_PROFILE, manifest)


def test_the_receipt_refuses_a_changed_pin() -> None:
    tampered = {**_receipt(), "package_url": "https://example.invalid/other.tgz"}
    with pytest.raises(lsp_identity.ManifestError) as raised:
        lsp_identity.validate_install_manifest(TYPESCRIPT_PROFILE, tampered)
    assert raised.value.code == "typescript_package_url_mismatch"


def test_the_neutral_seam_reproduces_pyrights_own_digests() -> None:
    """The profile is derived from Pyright's constants, so it cannot drift."""
    assert (
        lsp_identity.profile_initialization_options_sha256(PYRIGHT_PROFILE)
        == PYRIGHT_INITIALIZATION_OPTIONS_SHA256
    )
    assert (
        lsp_identity.profile_configuration_sha256(PYRIGHT_PROFILE)
        == PYRIGHT_CONFIGURATION_SHA256
    )


# -- one process per language ---------------------------------------------


def test_two_languages_in_one_checkout_do_not_share_a_session_slot(
    tmp_path: Path,
) -> None:
    scope = resolve_repository_scope(_repository(tmp_path))
    identity = _identity()
    python_key = LanguageServerSessionManager._profile_key(
        scope, identity, PYRIGHT_PROFILE
    )
    typescript_key = LanguageServerSessionManager._profile_key(
        scope, identity, TYPESCRIPT_PROFILE
    )
    assert python_key != typescript_key
    assert python_key[0] == typescript_key[0] == scope.checkout_id


# -- the readiness gate ----------------------------------------------------


def test_only_a_progress_gated_profile_waits_for_the_project_load() -> None:
    assert PYRIGHT_PROFILE.gates_on_progress() is False
    assert TYPESCRIPT_PROFILE.gates_on_progress() is True
    assert TYPESCRIPT_PROFILE.readiness == READINESS_WORK_DONE_PROGRESS


def _gated_session(tmp_path: Path) -> LanguageServerSession:
    scope = resolve_repository_scope(_repository(tmp_path))
    return LanguageServerSession(
        scope, _identity(), state_root=tmp_path / "state", profile=TYPESCRIPT_PROFILE
    )


def test_a_gated_session_is_not_query_ready_until_the_end_signal(
    tmp_path: Path,
) -> None:
    """The measured difference between a right and a wrong answer: 0/12 vs 12/12."""
    session = _gated_session(tmp_path)
    with session._lock:
        session._generation_nonce = "generation-1"
        assert session._progress_gate_satisfied_locked() is False

        session._work_done_tokens.add("load-token")
        assert session._progress_gate_satisfied_locked() is False

    session._progress("$/progress", {"token": "load-token", "value": {"kind": "begin", "title": "load"}})
    with session._lock:
        assert session._progress_gate_satisfied_locked() is False

    session._progress("$/progress", {"token": "load-token", "value": {"kind": "end"}})
    with session._lock:
        assert session._progress_gate_satisfied_locked() is True


def test_an_end_on_a_token_the_server_never_opened_does_not_open_the_gate(
    tmp_path: Path,
) -> None:
    session = _gated_session(tmp_path)
    with session._lock:
        session._generation_nonce = "generation-1"
    session._progress("$/progress", {"token": "stranger", "value": {"kind": "end"}})
    with session._lock:
        assert session._progress_gate_satisfied_locked() is False


def test_a_new_generation_must_load_the_project_again(tmp_path: Path) -> None:
    session = _gated_session(tmp_path)
    with session._lock:
        session._generation_nonce = "generation-1"
        session._work_done_tokens.add("load-token")
    session._progress("$/progress", {"token": "load-token", "value": {"kind": "end"}})
    with session._lock:
        assert session._progress_gate_satisfied_locked() is True
        session._generation_nonce = "generation-2"
        assert session._progress_gate_satisfied_locked() is False


def test_the_gate_never_delays_pyright(tmp_path: Path) -> None:
    scope = resolve_repository_scope(_repository(tmp_path))
    session = LanguageServerSession(
        scope, _identity(), state_root=tmp_path / "state", profile=PYRIGHT_PROFILE
    )
    with session._lock:
        assert session._progress_gate_satisfied_locked() is True
    started = time.monotonic()
    session._await_progress_gate(started + 5)
    assert time.monotonic() - started < 0.05


def test_the_work_done_token_registry_is_bounded(tmp_path: Path) -> None:
    session = _gated_session(tmp_path)
    for index in range(500):
        session._retain_work_done_token(f"token-{index}")
    with session._lock:
        assert len(session._work_done_tokens) <= 64


# -- what the answer says about itself ------------------------------------


class _StubSession:
    def __init__(self, profile, codes: tuple[str, ...]) -> None:
        self.profile = profile
        self.degradation_codes = codes


def test_the_answer_names_the_server_that_produced_it() -> None:
    data = {"provider": {"name": "pyright", "version": "6.0.0"}, "warnings": ()}
    named = mcp_server._navigation_named_by_session(
        dict(data), _StubSession(TYPESCRIPT_PROFILE, ())
    )
    assert named["provider"] == {"name": "typescript", "version": "6.0.0"}


def test_the_answer_carries_the_capability_limits_by_name() -> None:
    data = {
        "provider": {"name": "typescript", "version": None},
        "warnings": ("structural fallback appended",),
    }
    named = mcp_server._navigation_named_by_session(
        data, _StubSession(TYPESCRIPT_PROFILE, ("typescript_missing",))
    )
    assert named["warnings"] == (
        "structural fallback appended",
        "typescript_missing",
    )


def test_a_qualified_session_adds_no_warning_of_its_own() -> None:
    """Python with a working Pyright must be byte-for-byte what it always was."""
    data = {"provider": {"name": "pyright", "version": "1.1.411"}, "warnings": ()}
    named = mcp_server._navigation_named_by_session(
        dict(data), _StubSession(PYRIGHT_PROFILE, ())
    )
    assert named == {
        "provider": {"name": "pyright", "version": "1.1.411"},
        "warnings": (),
    }


def test_a_structural_answer_keeps_its_absent_provider() -> None:
    data = {"provider": {"name": None, "version": None}, "warnings": ()}
    named = mcp_server._navigation_named_by_session(
        dict(data), _StubSession(TYPESCRIPT_PROFILE, ())
    )
    assert named["provider"]["name"] is None


# -- the transport allowlist, and the gap in it ---------------------------


def test_the_transport_carries_every_notification_the_neutral_layer_needs() -> None:
    from lsp_protocol import SERVER_NOTIFICATIONS

    assert lsp_profiles.NEUTRAL_SERVER_NOTIFICATIONS <= SERVER_NOTIFICATIONS
    assert PYRIGHT_PROFILE.server_notifications <= SERVER_NOTIFICATIONS


@pytest.mark.xfail(
    strict=True,
    reason=(
        "measured 2026-08-29: `lsp_protocol.SERVER_NOTIFICATIONS` is a module-level "
        "allowlist and drops `$/typescriptVersion`, so the profile's post-initialize "
        "identity assertion is registered but never reached. Widening it means "
        "editing `scripts/lsp_protocol.py`, which the complexity gate refuses "
        "wholesale over roughly thirty pre-existing findings in the transport hot "
        "path. The pinned engine is still checked, one step earlier, by digest "
        "against the install receipt before the process starts."
    ),
)
def test_the_transport_carries_every_profiles_own_notifications() -> None:
    from lsp_protocol import SERVER_NOTIFICATIONS

    assert lsp_profiles.server_notification_union() <= SERVER_NOTIFICATIONS
