"""A `workspace/configuration` item without a section gets the asking server's settings.

The answer for a sectionless item was Pyright's configuration whatever profile
the session ran, so gopls, rust-analyzer and the TypeScript server were told
Pyright's settings. The specification leaves `section` optional, and the
reference clients answer a sectionless item with that client's own settings.
See docs/research/2026-09-27-a-sectionless-configuration-is-the-servers-own.md.
"""

from __future__ import annotations

import pytest
from llm_wiki.pyright_session_progress import configuration_result
from lsp_profiles import REGISTRY


@pytest.mark.parametrize("name", REGISTRY.names())
def test_a_sectionless_item_is_answered_with_the_profiles_own_settings(name: str) -> None:
    settings = REGISTRY.get(name).wire_configuration()

    assert configuration_result(settings, {}) == settings
    assert configuration_result(settings, {"scopeUri": "file:///srv/repo"}) == settings


@pytest.mark.parametrize("name", REGISTRY.names())
def test_a_named_section_is_read_from_the_profiles_own_settings(name: str) -> None:
    settings = REGISTRY.get(name).wire_configuration()
    top = next(iter(settings))

    assert configuration_result(settings, {"section": top}) == settings[top]
    assert configuration_result(settings, {"section": "no-such-section"}) is None
