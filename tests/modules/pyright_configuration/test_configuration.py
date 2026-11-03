"""The original pure configuration immutability tests."""
from __future__ import annotations

import pytest
from llm_wiki.pyright_configuration import (
    PYRIGHT_CONFIGURATION,
    PYRIGHT_INITIALIZATION_OPTIONS,
    thaw_pyright_profile_value,
)


def test_exported_pyright_configuration_is_recursively_immutable() -> None:
    analysis = PYRIGHT_CONFIGURATION["python"]["analysis"]
    original = analysis["logLevel"]
    try:
        with pytest.raises(TypeError):
            analysis["logLevel"] = "Information"
    finally:
        if isinstance(analysis, dict):
            analysis["logLevel"] = original
    assert PYRIGHT_CONFIGURATION["python"]["analysis"]["logLevel"] == "Error"


def test_exported_pyright_initialization_lists_are_immutable() -> None:
    exclude = PYRIGHT_INITIALIZATION_OPTIONS["files"]["exclude"]
    try:
        with pytest.raises((AttributeError, TypeError)):
            exclude.append("**/outside")
    finally:
        if isinstance(exclude, list):
            exclude.clear()
    assert thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS) == {
        "files": {"exclude": []}
    }



def test_wire_defaults_keep_the_published_fingerprints():
    from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
    from llm_wiki.pyright_configuration import (
        PYRIGHT_CONFIGURATION_SHA256,
        PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
    )

    configuration = thaw_pyright_profile_value(PYRIGHT_CONFIGURATION)
    initialization = thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS)
    assert sha256_bytes(canonical_json_bytes(configuration)) == PYRIGHT_CONFIGURATION_SHA256
    assert sha256_bytes(canonical_json_bytes(initialization)) == PYRIGHT_INITIALIZATION_OPTIONS_SHA256
    assert PYRIGHT_CONFIGURATION_SHA256 == 'bb4a710824a77070460988c5d4e871f3aec9ddbad599d20bdbe06850077974c1'
    assert PYRIGHT_INITIALIZATION_OPTIONS_SHA256 == 'cd6476e3527e6894d4ab2a159ad163645a9cfccb40117f0ac04b82ca46847129'


def test_thawed_nested_values_are_independent_between_callers():
    first = thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS)
    second = thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS)
    first['files']['exclude'].append('different')
    assert second == {'files': {'exclude': []}}
    assert PYRIGHT_INITIALIZATION_OPTIONS['files']['exclude'] == ()


@pytest.mark.parametrize('value', [1.5, [], b'bytes', object()])
def test_thaw_keeps_the_existing_restricted_scalar_domain(value):
    with pytest.raises(TypeError, match='unsupported Pyright profile value'):
        thaw_pyright_profile_value(value)
