"""Every declared path and byte survives exact-body deduplication."""
import pytest
from llm_wiki.context_bundle import pack_files, unpack_files


def test_identical_bodies_appear_once_but_both_paths_round_trip():
    body = ('Unique instruction body.\n' * 100).encode()
    files = {'AGENTS.md': body, 'CLAUDE.md': body}
    bundle = pack_files(files)
    assert bundle.count(body.decode()) == 1
    assert 'SAME ["CLAUDE.md","AGENTS.md"]' in bundle
    assert unpack_files(bundle) == files


def test_similar_bodies_are_never_merged():
    files = {'a': b'one\n', 'b': b'one\r\n', 'c': b'one ',
             'd': 'é'.encode(), 'e': 'e\u0301'.encode()}
    assert unpack_files(pack_files(files)) == files
    assert '\nSAME [' not in pack_files(files)


def test_empty_and_unicode_paths_and_embedded_headers_round_trip():
    files = {'empty': b'', 'dir/два слова.md': 'FILE ["x",0]\nSAME ["a","b"]\nПривет'.encode(),
             'quote"name': b'no final newline'}
    assert unpack_files(pack_files(files)) == files


def test_input_order_does_not_change_bundle():
    assert pack_files({'b': b'2', 'a': b'1'}) == pack_files({'a': b'1', 'b': b'2'})


@pytest.mark.parametrize('name', ['', '.', '..', '../a', '/a', 'a/../b', 'a//b', './a'])
def test_noncanonical_or_external_paths_are_refused(name):
    with pytest.raises(ValueError):
        pack_files({name: b'content'})


def test_invalid_utf8_is_not_replaced():
    with pytest.raises(UnicodeDecodeError):
        pack_files({'a': b'\xff'})


def _header():
    return pack_files({})


@pytest.mark.parametrize('record', [
    'OTHER ["a",0]\n', 'FILE ["a",-1]\n', 'FILE ["a",true]\n',
    'FILE ["a","1"]\n', 'FILE ["a",5]\nabc\n', 'FILE ["a",1]\nx',
    'FILE ["a",1]\nx!\n', 'FILE ["a"]\n', 'FILE {}\n',
    'SAME ["a","missing"]\n', 'SAME ["a",[]]\n',
    'FILE ["../a",0]\n\n', 'FILE ["a",0]\n\nFILE ["a",0]\n\n',
    'FILE ["a",0]', 'FILE ["a",0]\n\nSAME ["a","a"]\n'])
def test_malformed_or_incomplete_artifacts_are_refused(record):
    with pytest.raises(ValueError):
        unpack_files(_header() + record)


def test_unknown_version_is_refused():
    with pytest.raises(ValueError):
        unpack_files(pack_files({}).replace('exact-duplicates/v1', 'exact-duplicates/v2'))


def test_reference_can_only_use_previously_defined_file():
    with pytest.raises(ValueError):
        unpack_files(_header() + 'SAME ["a","b"]\nFILE ["b",0]\n\n')
