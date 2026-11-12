"""Helpers shared by test_compile_binds_the_protected_source_view.py and the test files importing them."""

import compile_pipeline
import compile_receipts
from llm_wiki import compile_pipeline_sources

from tests.llm_descriptors_helpers import _write_dlp_policy


def _policy(monkeypatch, tmp_path, literals=('private-word',)):
    path = tmp_path / 'dlp.json'
    _write_dlp_policy(path, literals=literals)
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(path))


def _legacy_packet(tmp_path, monkeypatch, lines):
    path = 'knowledge/daily/2026-09-25.md'
    raw = ('## [13:51:26] event\n' + '\n'.join(lines) + '\n').encode()
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    digest = compile_pipeline.sha256_bytes(raw)
    daily = compile_receipts.DailySnapshot(path, raw, digest)
    source = compile_receipts.SourceSnapshot(path, raw, digest)
    return compile_pipeline_sources.CompileInputs((daily,), (source,), ())
