"""Refreshed context preserves the whole view without copying unchanged bodies."""
import io
import zipfile
from contextlib import contextmanager
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest
from reliable_memory import sha256_bytes

from tests.test_compile_streamed_archive_batches import _archive


def _source(path, body):
    return compiler.SourceSnapshot(path, body, sha256_bytes(body))


def _context(body=b'accepted note'):
    source = _source('knowledge/notes/accepted.md', body)
    target = compiler.TargetSnapshot(source.logical_path, body, source.sha256)
    rule = _source('AGENTS.md', b'operating contract')
    return compiler.CompileInputs((), (rule, source), (target,), (rule,))


def _records(inputs):
    for group in ('sources', 'targets', 'vault_files'):
        for record in getattr(inputs, group):
            yield group, record


@contextmanager
def _delta(base, current):
    output = io.BytesIO()
    digest = controller.write_compile_context_delta(output, base, _records(current))
    with zipfile.ZipFile(output) as archive:
        view = controller.read_compile_context_delta(archive, digest, base)
        yield view, archive, output.getvalue()


def _materialized(view):
    return compiler.CompileInputs((), tuple(view.sources), tuple(view.targets), tuple(view.vault_files))


def test_unchanged_context_stores_no_duplicate_body():
    original = _context(b'x' * 100000)
    with _archive(original) as base:
        with _delta(base, original) as (view, archive, raw):
            assert not any(name.startswith('blobs/') for name in archive.namelist())
            assert len(raw) < len(original.sources[-1].content)
            assert _materialized(view) == original


def test_changed_added_and_deleted_records_restore_exact_context():
    original = _context()
    current = _context(b'updated rule')
    new = _source('knowledge/notes/new.md', b'new fact')
    current = replace(current, sources=(*current.sources[1:], new),
        targets=(*current.targets, compiler.TargetSnapshot(new.logical_path, new.content, new.sha256)),
        vault_files=())
    with _archive(original) as base:
        with _delta(base, current) as (view, _, _raw):
            assert _materialized(view) == current
            assert tuple(view.sources.select_paths({new.logical_path})) == (new,)


def test_empty_fresh_context_removes_the_old_context():
    with _archive(_context()) as base:
        with _delta(base, compiler.CompileInputs((), (), ())) as (view, _, _raw):
            assert _materialized(view) == compiler.CompileInputs((), (), ())


def test_context_delta_does_not_change_daily_records():
    from tests.test_compile_streamed_archive_batches import _many_days

    inputs = _many_days(2)
    with _archive(inputs) as base:
        with pytest.raises(ValueError, match='context.*group'):
            controller.write_compile_context_delta(io.BytesIO(), base, [('dailies', inputs.dailies[0])])


def test_a_different_base_cannot_supply_unchanged_bytes():
    original = _context()
    with _archive(original) as base:
        with _delta(base, original) as (_, archive, raw):
            digest = sha256_bytes(archive.read('manifest.json'))
            with _archive(_context(b'other bytes')) as other:
                with zipfile.ZipFile(io.BytesIO(raw)) as delta:
                    with pytest.raises(ValueError, match='context.*base'):
                        controller.read_compile_context_delta(delta, digest, other)


def test_duplicate_current_path_is_rejected():
    original = _context()
    repeated = [*_records(original), ('sources', original.sources[0])]
    with _archive(original) as base:
        with pytest.raises(ValueError, match='duplicate.*context'):
            controller.write_compile_context_delta(io.BytesIO(), base, repeated)


def test_delta_reader_does_not_load_unchanged_bodies_until_requested(monkeypatch):
    original = _context()
    with _archive(original) as base:
        with _delta(base, original) as (_, archive, _raw):
            digest = sha256_bytes(archive.read('manifest.json'))

            def forbidden(*_args):
                raise AssertionError('unchanged body read eagerly')

            monkeypatch.setattr(base, 'blob', forbidden)
            view = controller.read_compile_context_delta(archive, digest, base)
            assert view.sources.logical_paths() == ('AGENTS.md', 'knowledge/notes/accepted.md')
            assert len(view.targets) == 1


def _rewrite_delta(raw, transform):
    import json

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(entries['manifest.json'])
    transform(manifest, entries)
    entries['manifest.json'] = controller._snapshot_json(manifest)
    with zipfile.ZipFile(output, 'w') as target:
        for name, body in entries.items():
            target.writestr(name, body)
    return output.getvalue(), sha256_bytes(entries['manifest.json'])


@pytest.mark.parametrize('removed', [
    ['missing.md'], ['AGENTS.md', 'AGENTS.md'], ['z.md', 'a.md'], [None], 'AGENTS.md',
])
def test_invalid_removal_manifest_is_rejected(removed):
    original = _context()
    with _archive(original) as base:
        with _delta(base, original) as (_, _, raw):
            changed, digest = _rewrite_delta(raw,
                lambda manifest, _entries: manifest['removed'].update(sources=removed))
        with zipfile.ZipFile(io.BytesIO(changed)) as archive:
            with pytest.raises(ValueError, match='removed context'):
                controller.read_compile_context_delta(archive, digest, base)


def test_changed_record_cannot_also_be_removed():
    with _archive(_context()) as base:
        with _delta(base, _context(b'changed')) as (_, _, raw):
            changed, digest = _rewrite_delta(raw, lambda manifest, _entries:
                manifest['removed'].update(sources=['knowledge/notes/accepted.md']))
        with zipfile.ZipFile(io.BytesIO(changed)) as archive:
            with pytest.raises(ValueError, match='changed and removed'):
                controller.read_compile_context_delta(archive, digest, base)


def test_changed_body_corruption_is_detected_on_access():
    with _archive(_context()) as base:
        with _delta(base, _context(b'changed')) as (_, _, raw):
            changed, digest = _rewrite_delta(raw, lambda _manifest, entries:
                entries.update({'blobs/' + sha256_bytes(b'changed'): b'corrupted'}))
        with zipfile.ZipFile(io.BytesIO(changed)) as archive:
            view = controller.read_compile_context_delta(archive, digest, base)
            with pytest.raises(ValueError, match='digest'):
                tuple(view.targets)


def test_prompt_projection_changes_even_when_physical_source_is_unchanged():
    original = _context()
    projected = replace(original.sources[-1], prompt_content=b'new projection')
    current = replace(original, sources=(original.sources[0], projected))
    with _archive(original) as base:
        with _delta(base, current) as (view, _, _raw):
            assert _materialized(view) == current


def test_context_delta_is_deterministic_and_not_a_complete_capture():
    with _archive(_context()) as base:
        with _delta(base, _context(b'changed')) as (_, _, first):
            pass
        with _delta(base, _context(b'changed')) as (_, archive, second):
            assert first == second
            digest = sha256_bytes(archive.read('manifest.json'))
            with pytest.raises(ValueError, match='schema'):
                controller.CompileInputArchive(archive, digest)
