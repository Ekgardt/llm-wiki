"""Source identity invariants, including distinct migration/failure policies."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from llm_wiki import queue_sources as source

DIGEST = 'a' * 64
PATH = 'knowledge/daily/2026-11-02.md'


@pytest.mark.parametrize('daily', ['2024-02-29', '2026-11-02'])
def test_real_canonical_dates(daily):
    assert source.validate_source_identity(daily, DIGEST) is None


@pytest.mark.parametrize('daily', [None, 20261102, '', '2026-2-02', '2026-02-29', '2026-13-01', '２０２６-１１-０２'])
def test_noncanonical_dates_refused(daily):
    with pytest.raises(ValueError, match='canonical date'):
        source.require_canonical_daily_id(daily)


@pytest.mark.parametrize('digest', [None, 0, '', 'A' * 64, 'a' * 63, 'g' * 64])
def test_bad_digests_refused(digest):
    with pytest.raises(ValueError, match='lowercase SHA-256'):
        source.validate_source_identity('2026-11-02', digest)


@pytest.mark.parametrize('path', ['knowledge/notes/a.md', '../daily/2026-11-02.md', '/knowledge/daily/2026-11-02.md', 'knowledge/daily/2026-02-29.md'])
def test_failure_identity_only_accepts_real_daily_sources(path):
    assert source.validated_failure_identity(path, DIGEST) is None


def test_nested_keys_are_case_insensitive_and_ordered():
    payload = {'SOURCE_PATH': PATH, 'items': [{'HASH': DIGEST}, {'daily_id': '2026-11-02'}, {'logical_path': PATH}]}
    assert source.source_identity_strings(payload) == {
        'paths': [PATH, PATH], 'digests': [DIGEST], 'daily_ids': ['2026-11-02']}
    assert source.distinct_source_identities(payload)['paths'] == {PATH}


def test_only_dict_and_list_are_traversed():
    assert source.source_identity_strings(({'hash': DIGEST},)) == {
        'paths': [], 'digests': [], 'daily_ids': []}


def test_daily_fallback_does_not_overwrite_explicit_path():
    assert source.distinct_source_identities({'daily_id': '2026-11-02'})['paths'] == {PATH}
    found = source.distinct_source_identities({'source_path': 'chosen', 'daily_id': '2026-11-02'})
    assert found['paths'] == {'chosen'}


def test_ambiguous_daily_fallback_is_not_invented():
    payload = [{'daily_id': '2026-11-02'}, {'daily_id': '2026-11-03'}]
    assert source.distinct_source_identities(payload)['paths'] == set()


def test_failure_recording_retains_first_encountered_identity():
    payload = [{'source_path': PATH, 'hash': DIGEST}, {'source_path': 'knowledge/daily/2026-11-03.md', 'hash': 'b' * 64}]
    assert source.source_identity_from_payload(json.dumps(payload)) == (PATH, DIGEST)


@pytest.mark.parametrize('payload', ['{', '{}', 'null', '{"source_path": "invalid", "hash": "a"}'])
def test_unusable_failure_payload_has_no_identity(payload):
    assert source.source_identity_from_payload(payload) is None


def test_payload_fields_keep_daily_ids_separate():
    assert source.payload_identity_fields('{') is None
    assert source.payload_identity_fields('{"daily_id":"2026-11-02"}') == {
        'paths': set(), 'digests': set(), 'daily_ids': {'2026-11-02'}}


@pytest.mark.parametrize('path', ['', '/a', '../a', 'a//b', 'a/./b', 'a\\b', 'x' * 4097, None])
def test_unsafe_general_links_refused(path):
    with pytest.raises(ValueError, match='source link path'):
        source.normalized_source_link((path, DIGEST))


def test_general_links_accept_nondaily_sources_and_preserve_order():
    links = [('knowledge/notes/a.md', DIGEST), (PATH, DIGEST)]
    assert source.normalized_source_links(links) == links


def test_duplicates_are_refused():
    with pytest.raises(ValueError, match='unique'):
        source.normalized_source_links([(PATH, DIGEST), (PATH, DIGEST)])


@pytest.mark.parametrize('item', [[PATH, DIGEST], (PATH,), None])
def test_links_require_tuple_pairs(item):
    with pytest.raises(ValueError, match='pairs'):
        source.normalized_source_link(item)


def test_import_is_independent_of_executors():
    program = '''
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname not in {'memory_queue','reliable_memory','markdown_transaction','compile_progress'}
sys.meta_path.insert(0, Deny())
from llm_wiki.queue_sources import validate_source_identity
validate_source_identity('2026-11-02', 'a' * 64)
'''
    result = subprocess.run([sys.executable, '-c', program, str(Path(__file__).resolve().parents[3] / 'scripts')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_strict_parser_keeps_migration_error_identity():
    from llm_wiki.sqlite_contracts import OperationalDatabaseContractError
    with pytest.raises(OperationalDatabaseContractError) as caught:
        source.queue_v3_source_links(json.dumps({'source_path': PATH}).encode())
    assert caught.value.code == 'queue_v2_source_identity_ambiguous'


@pytest.mark.parametrize('payload', [b'{', b'\xff', b'{}'])
def test_unreadable_or_absent_migration_identity_has_no_links(payload):
    assert source.queue_v3_source_links(payload) == ()


def test_strict_parser_keeps_daily_fallback_and_digest():
    payload = json.dumps({'daily_id': '2026-11-02', 'hash': DIGEST}).encode()
    assert source.queue_v3_source_links(payload) == ((PATH, DIGEST),)
