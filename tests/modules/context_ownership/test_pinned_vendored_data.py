"""Vendored third-party data is covered only while its bytes equal a declared pin."""
import hashlib
import inspect
import json

import pytest
import tiktoken_ext.openai_public as openai_public

from tests import module_ownership
from tests.module_context import VENDORED_ENCODINGS
from tests.module_ownership import ownership_report

SYNTHETIC_URL = 'https://example.invalid/encodings/synthetic.tiktoken'
SYNTHETIC_BYTES = b'c3ludGhldGlj 0\n'
VENDORED_NAME = f'tests/data/tiktoken/{hashlib.sha1(SYNTHETIC_URL.encode()).hexdigest()}'


def _write(root, name, content=b''):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _repository(root, vocabulary=SYNTHETIC_BYTES):
    manifest = 'docs/modules/one-context.json'
    _write(root, manifest, json.dumps({'files': [manifest], 'ownership': {'files': [manifest]}}).encode())
    _write(root, VENDORED_NAME, vocabulary)
    return {manifest, VENDORED_NAME}


@pytest.fixture
def synthetic_pin(monkeypatch):
    pin = {'synthetic': (SYNTHETIC_URL, hashlib.sha256(SYNTHETIC_BYTES).hexdigest())}
    monkeypatch.setattr(module_ownership, 'PINNED_VOCABULARIES', pin)


def test_a_pinned_vocabulary_completes_ownership_without_a_module(tmp_path, synthetic_pin):
    report = ownership_report(tmp_path, tracked=_repository(tmp_path))
    assert report['complete'] is True
    assert report['pinned_vendored_data'] == [VENDORED_NAME]
    assert VENDORED_NAME not in report['ownership']
    assert (report['owned_count'], report['pinned_count'], report['tracked_count']) == (1, 1, 2)


def test_an_untracked_file_beside_the_vocabulary_is_not_inventory(tmp_path, synthetic_pin):
    tracked = _repository(tmp_path)
    _write(tmp_path, 'tests/data/tiktoken/.DS_Store', b'finder')
    _write(tmp_path, f'{VENDORED_NAME}.0f3e.tmp', b'partial download')
    report = ownership_report(tmp_path, tracked=tracked)
    assert report['complete'] is True
    assert report['pinned_vendored_data'] == [VENDORED_NAME]


def test_a_changed_vocabulary_is_refused_and_left_in_place(tmp_path, synthetic_pin):
    tracked = _repository(tmp_path, vocabulary=b'dGFtcGVyZWQ= 0\n')
    with pytest.raises(AssertionError, match='Vendored synthetic differs from its pin'):
        ownership_report(tmp_path, tracked=tracked)
    assert (tmp_path / VENDORED_NAME).read_bytes() == b'dGFtcGVyZWQ= 0\n'


def test_an_unpinned_file_beside_the_vocabulary_is_refused(tmp_path, synthetic_pin):
    tracked = _repository(tmp_path) | {'tests/data/tiktoken/extra'}
    _write(tmp_path, 'tests/data/tiktoken/extra', b'payload')
    with pytest.raises(AssertionError, match=r"Unpinned vendored vocabulary: \['extra'\]"):
        ownership_report(tmp_path, tracked=tracked)


def test_a_vocabulary_claimed_by_a_module_is_refused(tmp_path, synthetic_pin):
    tracked = _repository(tmp_path)
    manifest = 'docs/modules/one-context.json'
    data = {'files': [manifest, VENDORED_NAME], 'ownership': {'files': [manifest, VENDORED_NAME]}}
    _write(tmp_path, manifest, json.dumps(data).encode())
    with pytest.raises(AssertionError, match='Vendored data also claimed'):
        ownership_report(tmp_path, tracked=tracked)


def test_the_pin_is_the_one_the_installed_tokenizer_verifies():
    url, digest = module_ownership.PINNED_VOCABULARIES['o200k_base']
    source = inspect.getsource(openai_public.o200k_base)
    assert f'"{url}"' in source
    assert f'expected_hash="{digest}"' in source


def test_measurement_reads_the_vocabulary_the_ownership_pin_covers():
    root = VENDORED_ENCODINGS.parents[2]
    assert VENDORED_ENCODINGS == root / module_ownership.VENDORED_VOCABULARIES
