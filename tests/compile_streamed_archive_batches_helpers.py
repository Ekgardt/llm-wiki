"""Helpers shared by test_compile_streamed_archive_batches.py and the test files importing them."""

import io
import zipfile
from contextlib import contextmanager
from dataclasses import replace

import compile_controller as controller
from bounded_io import VerifiedByteRange
from llm_wiki import compile_pipeline_sources

from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import _inputs


@contextmanager
def _archive(inputs):
    output = io.BytesIO()
    digest = controller.compile_inputs_digest(inputs)
    controller.write_archive_snapshot(inputs, digest, output)
    with zipfile.ZipFile(output) as archive:
        yield controller.CompileInputArchive(archive, digest)


def _materialized(batch):
    inputs = batch.inputs
    return replace(batch, inputs=replace(inputs, dailies=tuple(_materialized_daily(part) for part in inputs.dailies),
                                         sources=tuple(inputs.sources),
                                         targets=tuple(inputs.targets),
                                         vault_files=tuple(inputs.vault_files)))


def _materialized_daily(part):
    if type(part.original_content) is VerifiedByteRange:
        part = replace(part, original_content=part.original_content[:])
    return replace(part, native_frames=tuple(part.native_frames))


def _many_days(count=8):
    first = _inputs()
    daily = first.dailies[0]
    source = first.sources[0]
    paths = [f'knowledge/daily/2026-10-{day:02}.md' for day in range(1, count + 1)]
    return compile_pipeline_sources.CompileInputs(tuple(replace(daily, logical_path=path) for path in paths),
                                  tuple(replace(source, logical_path=path) for path in paths), ())
