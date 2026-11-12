"""Helpers shared by test_compile_verified_original_ranges.py and the test files importing them."""

import hashlib
import zipfile
from contextlib import contextmanager

import compile_controller as controller
import compile_receipts
from llm_wiki import compile_pipeline_sources

from tests.compile_protected_dispatch_helpers import _archive_members
from tests.compile_streamed_capture_validation_helpers import _packed


@contextmanager
def _original(raw, deadline=None):
    digest = hashlib.sha256(raw).hexdigest()
    source = compile_receipts.SourceSnapshot('knowledge/daily/test.md', raw, digest)
    members = _archive_members(compile_pipeline_sources.CompileInputs((), (source,), ()))
    expected = hashlib.sha256(members['manifest.json']).hexdigest()
    with zipfile.ZipFile(_packed(members)) as archive:
        reader = controller.CompileInputArchive(archive, expected)
        with reader.original_range(digest, deadline=deadline) as content:
            yield content
