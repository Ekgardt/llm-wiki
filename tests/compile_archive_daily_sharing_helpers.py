"""Helpers shared by test_compile_archive_daily_sharing.py and the test files importing them."""

import hashlib
import io
import zipfile
from contextlib import contextmanager

import compile_controller as controller
import compile_receipts
from llm_wiki import compile_pipeline_sources


def _parts(path):
    original = b'first entry\nsecond entry\n'
    digest = hashlib.sha256(original).hexdigest()
    return tuple(compile_receipts.DailySnapshot(
        path, body, hashlib.sha256(body).hexdigest(), index, 2,
        start, start + len(body), original, digest,
    ) for index, (start, body) in enumerate(((0, original[:12]), (12, original[12:]))))


@contextmanager
def _reader(parts):
    inputs = compile_pipeline_sources.CompileInputs(parts, (), ())
    output = io.BytesIO()
    digest = controller.compile_inputs_digest(inputs)
    controller.write_archive_snapshot(inputs, digest, output)
    with zipfile.ZipFile(output) as archive:
        yield controller.CompileInputArchive(archive, digest)
