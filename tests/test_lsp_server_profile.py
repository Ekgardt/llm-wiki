"""The language-neutral seam, and that Pyright through it is the same Pyright.

The point of these tests is not that the seam works -- it is that expressing the
existing Pyright path through the seam changes nothing. Every assertion about
Pyright below is written against the constants and construction that governed it
before this module existed, so the equivalence cannot be satisfied by editing
the seam to agree with itself.
"""

from __future__ import annotations

import json

import lsp_profiles
import lsp_protocol
import pyright_profile
import pyright_session

from tests.modules.lsp_server_profile.paths import STATE_ROOT


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


# --------------------------------------------------------------------------
# Pyright through the seam is byte-for-byte the Pyright that already shipped.
# --------------------------------------------------------------------------




def test_the_pyright_configuration_sent_over_the_wire_is_unchanged():
    profile = lsp_profiles.PYRIGHT_PROFILE
    already_shipped = pyright_profile.thaw_pyright_profile_value(
        pyright_profile.PYRIGHT_CONFIGURATION
    )
    assert _canonical(profile.wire_configuration()) == _canonical(already_shipped)


def test_the_pyright_initialization_options_are_unchanged_and_take_no_runtime_path():
    profile = lsp_profiles.PYRIGHT_PROFILE
    already_shipped = pyright_profile.thaw_pyright_profile_value(
        pyright_profile.PYRIGHT_INITIALIZATION_OPTIONS
    )
    assert profile.runtime_option is None
    assert _canonical(profile.wire_initialization_options(STATE_ROOT)) == _canonical(
        already_shipped
    )


def test_the_pyright_profile_pins_the_artifact_the_installer_already_pins():
    profile = lsp_profiles.PYRIGHT_PROFILE
    assert profile.package_url == pyright_profile.PYRIGHT_PACKAGE_URL
    assert profile.package_integrity == pyright_profile.PYRIGHT_PACKAGE_INTEGRITY
    assert profile.server_relative == pyright_profile.PYRIGHT_SERVER_RELATIVE
    assert profile.node_major == pyright_profile.QUALIFIED_NODE_MAJOR


def test_the_pyright_vendor_notifications_are_the_ones_the_protocol_allows():
    """These three names are hardcoded in lsp_protocol today; the profile owns them."""
    hardcoded = {
        name
        for name in lsp_protocol.SERVER_NOTIFICATIONS
        if name.startswith("pyright/")
    }
    assert lsp_profiles.PYRIGHT_PROFILE.server_notifications == hardcoded


def test_the_existing_client_already_advertises_work_done_progress():
    """The readiness gate a second server needs is reachable with today's client.

    If this ever goes false, the TypeScript profile's readiness policy becomes
    unreachable and it will answer before its project loads.
    """
    assert pyright_session.CLIENT_CAPABILITIES["window"]["workDoneProgress"] is True


def test_the_protocol_already_accepts_the_progress_create_request():
    assert "window/workDoneProgress/create" in lsp_protocol.SERVER_REQUESTS
    assert "$/progress" in lsp_protocol.SERVER_NOTIFICATIONS


# --------------------------------------------------------------------------
# TypeScript: the three fields that exist because a second language forced them.
# --------------------------------------------------------------------------




















# --------------------------------------------------------------------------
# The registry routes by suffix, and refuses to guess.
# --------------------------------------------------------------------------
















# --------------------------------------------------------------------------
# The seam refuses what it cannot honour.
# --------------------------------------------------------------------------
















# --------------------------------------------------------------------------
# Freezing is what stops a caller mutating another session's profile.
# --------------------------------------------------------------------------
