"""Unchanged pure profile validation and immutability cases."""
from __future__ import annotations

from pathlib import Path

import pytest
from lsp_server_profile import (
    IdentityNotification,
    ProfileError,
    RuntimeOption,
    freeze_profile_value,
    thaw_profile_value,
)

from tests.modules.lsp_server_profile.paths import STATE_ROOT, _abs
from tests.modules.lsp_server_profile.values_helpers import _profile


def test_a_profile_declares_one_runtime_and_only_one():
    with pytest.raises(ProfileError):
        _profile(node_major=None)
    with pytest.raises(ProfileError):
        _profile(native=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"package_url": "http://example.invalid/a.tgz"},
        {"package_integrity": "sha1-AAAA"},
        {"server_relative": _abs("absolute", "main.js")},
        {"server_relative": Path("../escape/main.js")},
        {"managed_relative_root": Path("../../etc")},
        {"readiness": "whenever"},
        {"node_major": 0},
        {"node_minor_floor": -1},
        {"file_suffixes": ()},
        {"language_ids": ()},
    ],
)
def test_an_unhonourable_profile_is_refused_at_construction(overrides):
    with pytest.raises(ProfileError):
        _profile(**overrides)


def test_a_relative_launch_path_is_refused():
    with pytest.raises(ProfileError):
        _profile().launch_command(Path("node"), _abs("a", "b.js"), _abs("c"))


def test_a_runtime_option_will_not_overwrite_a_non_object():
    option = RuntimeOption(key_path=("a", "b"), sibling_relative=Path("t.js"))
    with pytest.raises(ProfileError):
        option.applied({"a": "already a string"}, STATE_ROOT / "managed")


def test_a_runtime_option_creates_the_branch_it_needs():
    option = RuntimeOption(key_path=("a", "b", "c"), sibling_relative=Path("t.js"))
    managed = _abs("srv", "managed")
    result = option.applied({}, managed)
    assert result == {"a": {"b": {"c": str(managed / "t.js")}}}


def test_a_runtime_option_needs_an_absolute_managed_root():
    option = RuntimeOption(key_path=("a",), sibling_relative=Path("t.js"))
    with pytest.raises(ProfileError):
        option.applied({}, Path("relative/managed"))


def test_an_identity_notification_needs_every_field_named():
    with pytest.raises(ProfileError):
        IdentityNotification(
            method="", version_key="v", source_key="s", required_source="u"
        )


def test_a_frozen_profile_value_cannot_be_mutated():
    frozen = freeze_profile_value({"a": {"b": [1, 2]}})
    with pytest.raises(TypeError):
        frozen["a"] = 1
    assert frozen["a"]["b"] == (1, 2)


def test_thawing_returns_a_plain_mutable_copy():
    frozen = freeze_profile_value({"a": {"b": [1, 2]}})
    thawed = thaw_profile_value(frozen)
    thawed["a"]["b"].append(3)
    assert frozen["a"]["b"] == (1, 2)


def test_an_unsupported_profile_value_is_refused():
    with pytest.raises(ProfileError):
        freeze_profile_value({"a": object()})
