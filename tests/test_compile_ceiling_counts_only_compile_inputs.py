"""Health forecasts the compiler's inputs, rather than the receipt directory."""

from pathlib import Path

import compile_memory
import doctor
import memory_state


def _vault(root: Path, metadata: bool) -> list[Path]:
    sources = {
        'knowledge/notes/current.md': b'---\ntype: concept\n---\n# Current\n',
        'knowledge/daily/2026-10-01.md': b'# 2026-10-01\n\nOne observed entry.\n',
        'docs/AGENTS.md': b'Rules for this vault.\n',
    }
    if metadata:
        sources.update({
            'knowledge/index.md': b'# Index\n',
            'knowledge/log.local.md': b'Local editorial log.\n',
        })
    ignored = {
        'knowledge/daily/README.md': b'Ancillary documentation.\n',
        'knowledge/daily/receipts/receipt.md': b'x' * 4096,
        'knowledge/notes/archive/old.md': b'x' * 4096,
        'AGENTS.md': b'A fallback not selected while docs/AGENTS.md exists.\n',
    }
    for relative, data in (sources | ignored).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return [root / relative for relative in sources]


def test_receipts_and_ancillary_files_do_not_inflate_the_source_count(tmp_path):
    _vault(tmp_path, metadata=False)
    (tmp_path / 'llm-wiki.toml').write_text('[compile]\nmax_sources = 5\n')
    near = doctor._settings_check(tmp_path)['details']['near_ceiling']
    assert 'compile.max_sources' not in near


def test_the_forecast_includes_the_metadata_the_compiler_reads(tmp_path):
    _vault(tmp_path, metadata=True)
    (tmp_path / 'llm-wiki.toml').write_text('[compile]\nmax_sources = 6\n')
    near = doctor._settings_check(tmp_path)['details']['near_ceiling']
    assert near['compile.max_sources'] == {'used': 5, 'ceiling': 6, 'share': 0.833}


def _bind_compiler(monkeypatch, root: Path) -> None:
    paths = {
        'ROOT': root,
        'MEMORY': root / 'knowledge',
        'DAILY_DIR': root / 'knowledge/daily',
        'KNOWLEDGE': root / 'knowledge/notes',
        'AGENTS': root / 'docs/AGENTS.md',
        'INDEX': root / 'knowledge/index.md',
        'LOG': root / 'knowledge/log.local.md',
    }
    for name, path in paths.items():
        monkeypatch.setattr(compile_memory, name, path)
    monkeypatch.setattr(memory_state, 'ROOT', root)


def test_forecast_bytes_equal_the_actual_readonly_compile_snapshot(tmp_path, monkeypatch):
    sources = _vault(tmp_path, metadata=True)
    total = sum(path.stat().st_size for path in sources)
    (tmp_path / 'llm-wiki.toml').write_text(
        f'[compile]\nmax_sources = 6\nmax_total_source_bytes = {total}\n'
    )
    _bind_compiler(monkeypatch, tmp_path)
    inputs = compile_memory.snapshot_compile_inputs(compile_memory._canonical_dailies())
    near = doctor._settings_check(tmp_path)['details']['near_ceiling']
    assert len(inputs.sources) == 5
    assert sum(len(source.content) for source in inputs.sources) == total
    assert near['compile.max_total_source_bytes'] == {
        'used': total, 'ceiling': total, 'share': 1.0,
    }
