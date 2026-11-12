"""Helpers shared by test_compile_packing_counts_original_entry_metadata.py and the test files importing them."""

from dataclasses import replace

import compile_receipts
from llm_wiki import compile_pipeline_sources
from reliable_memory import sha256_bytes


def _inputs(legacy=False, optional=False):
    path = "knowledge/daily/2026-10-02.md"
    content = "## [10:00:00] session-end | manual\nПолный факт о велосипеде.\n".encode()
    part = compile_pipeline_sources.daily_parts(path, content)[0]
    if legacy:
        part = replace(part, original_content=None, original_sha256="", original_entries=())
    source = compile_receipts.SourceSnapshot(path, content, sha256_bytes(content))
    context = compile_receipts.SourceSnapshot("knowledge/notes/évidence.md", b"context", sha256_bytes(b"context"))
    sources = (source, context) if optional else (source,)
    return compile_pipeline_sources.CompileInputs((part,), sources, ())
