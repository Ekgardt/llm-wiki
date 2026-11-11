"""Helpers shared by test_values.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

from lsp_server_profile import READINESS_INITIALIZED, LanguageServerProfile, freeze_profile_value


def _profile(**overrides) -> LanguageServerProfile:
    fields = {
        "name": "x",
        "language_ids": ("x",),
        "file_suffixes": (".x",),
        "version": "1",
        "package_url": "https://example.invalid/a.tgz",
        "package_integrity": "sha512-AAAA",
        "server_relative": Path("package/main.js"),
        "managed_relative_root": Path("cache/code-tools/x/1"),
        "install_manifest_schema": "x/v1",
        "node_major": 22,
        "launch_flags": ("--stdio",),
        "server_notifications": frozenset(),
        "configuration": freeze_profile_value({}),
        "initialization_options": freeze_profile_value({}),
        "readiness": READINESS_INITIALIZED,
        "degradation_prefix": "x",
    }
    fields.update(overrides)
    return LanguageServerProfile(**fields)
