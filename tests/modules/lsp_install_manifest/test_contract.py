"""Compare the transferred codec with recorded original receipt values."""
import json
from pathlib import Path

import pytest
from llm_wiki import lsp_install_manifest as manifest
from lsp_server_profile import RuntimeOption, freeze_profile_value

from tests.modules.lsp_server_profile.test_values import _profile

BASELINE = json.loads(Path(__file__).with_name('baseline.json').read_text())
CASES = BASELINE['cases']


def _runtime(value):
    if value is None:
        return None
    return RuntimeOption(**{**value, 'key_path': tuple(value['key_path']),
                            'sibling_relative': Path(value['sibling_relative'])})


def _receipt_profile(case):
    fields = dict(case['profile_fields'])
    fields['server_relative'] = Path(fields['server_relative'])
    fields['configuration'] = freeze_profile_value(fields['configuration'])
    fields['initialization_options'] = freeze_profile_value(fields['initialization_options'])
    return _profile(**fields, runtime_option=_runtime(case['runtime']))


@pytest.mark.parametrize('case', CASES, ids=[c['profile_fields']['name'] for c in CASES])
def test_original_profile_receipt_values(case):
    profile = _receipt_profile(case)
    value = manifest.build_install_manifest(profile, server_sha256='a' * 64,
                                            runtime_sha256=case['runtime_sha256'])
    assert value == case['expected']
    assert manifest.validate_install_manifest(profile, value) == case['validated']


@pytest.mark.parametrize('field,code', manifest._MANIFEST_FIELD_CODES)
def test_each_changed_declared_field_retains_its_error(field, code):
    case = CASES[0]
    value = {**case['expected'], field: 'changed'}
    profile = _receipt_profile(case)
    with pytest.raises(manifest.ManifestError) as raised:
        manifest.validate_install_manifest(profile, value)
    assert raised.value.code == profile.degradation_code(code)


@pytest.mark.parametrize('value', [None, [], {}, {'server_sha256': 'a' * 64}])
def test_malformed_shape_is_not_a_qualified_receipt(value):
    with pytest.raises(manifest.ManifestError) as raised:
        manifest.validate_install_manifest(_receipt_profile(CASES[0]), value)
    assert raised.value.code == 'manifest_malformed'


@pytest.mark.parametrize('digest', ['', 'A' * 64, 'a' * 63, None])
def test_builder_keeps_its_digest_validation(digest):
    with pytest.raises(ValueError, match='server_sha256 must be a lowercase SHA-256 digest'):
        manifest.build_install_manifest(_receipt_profile(CASES[0]), server_sha256=digest)


def test_validated_output_is_a_fresh_sorted_mapping():
    case = CASES[0]
    value = dict(case['expected'])
    result = manifest.validate_install_manifest(_receipt_profile(case), value)
    assert result is not value
    assert list(result) == sorted(value)
    assert value == case['expected']


@pytest.mark.parametrize('update', [{'extra': 'value'}, {'version': None}, {'server_sha256': 'bad'}])
def test_closed_shape_and_string_digest_rules_remain(update):
    with pytest.raises(manifest.ManifestError) as raised:
        manifest.validate_install_manifest(_receipt_profile(CASES[0]), {**CASES[0]['expected'], **update})
    assert raised.value.code == 'manifest_malformed'
