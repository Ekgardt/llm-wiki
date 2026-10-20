"""Required context preserves global ambiguity without retaining source bodies."""
import io
import weakref
import zipfile
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_keeps_physically_overlapping_existing_context import (
    _overlap_inputs,
    _part_version_inputs,
    _repeated_part_inputs,
)
from tests.test_compile_keeps_physically_overlapping_existing_context import (
    vault as vault,
)


def test_required_context_releases_input_snapshot_and_resolver(vault, monkeypatch):
    inputs, _ = _overlap_inputs(vault[0])
    captured = weakref.ref(inputs)
    resolvers = []
    original = compiler.EvidenceResolver

    def observe(*args, **kwargs):
        resolver = original(*args, **kwargs)
        resolvers.append(weakref.ref(resolver))
        return resolver

    monkeypatch.setattr(compiler, 'EvidenceResolver', observe)
    result = compiler._RequiredCitationContext(inputs, None)
    del inputs
    assert result.paths
    assert captured() is None
    assert resolvers
    assert all(reference() is None for reference in resolvers)


@pytest.mark.parametrize('fixture', [_overlap_inputs, _part_version_inputs, _repeated_part_inputs])
def test_archive_context_matches_complete_global_citation_check(vault, fixture):
    inputs, _ = fixture(vault[0])
    expected = compiler._RequiredCitationContext(inputs, None)
    output = io.BytesIO()
    digest = controller.compile_inputs_digest(inputs)
    controller._write_archive_snapshot(inputs, digest, output)
    with zipfile.ZipFile(output) as archive:
        reader = controller.CompileInputArchive(archive, digest)
        actual = reader.required_context()
        assert actual.paths == expected.paths
        assert actual.unresolved == expected.unresolved


def test_streamed_context_does_not_retain_replayed_daily_parts(vault):
    inputs, _ = _overlap_inputs(vault[0])
    references = []

    def records():
        for part in inputs.dailies:
            copied = replace(part)
            references.append(weakref.ref(copied))
            yield copied

    context = compiler._RequiredCitationContext(inputs, None, daily_records=records)
    assert context.paths
    assert len(references) == 2 * len(inputs.dailies)
    assert all(reference() is None for reference in references)


def test_replayed_daily_identity_change_is_refused(vault):
    inputs, _ = _overlap_inputs(vault[0])
    passes = []

    def records():
        passes.append(None)
        if len(passes) == 1:
            return iter(inputs.dailies)
        return iter(replace(part, original_sha256='0' * 64) for part in inputs.dailies)

    with pytest.raises(ValueError, match='daily.*changed'):
        compiler._RequiredCitationContext(inputs, None, daily_records=records)


@pytest.mark.parametrize('change', ['omit', 'append', 'part_key'])
def test_replayed_daily_membership_change_is_refused(vault, change):
    inputs, _ = _overlap_inputs(vault[0])
    passes = []
    changed = {
        'omit': (),
        'append': (*inputs.dailies, inputs.dailies[0]),
        'part_key': tuple(replace(part, part_count=2) for part in inputs.dailies),
    }[change]

    def records():
        passes.append(None)
        if len(passes) == 1:
            return iter(inputs.dailies)
        return iter(changed)

    with pytest.raises(ValueError, match='daily.*changed'):
        compiler._RequiredCitationContext(inputs, None, daily_records=records)
