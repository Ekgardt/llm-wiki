"""Health counts the claim tree, including its refusal boundary, not project journals."""
from pathlib import Path

import claim_tree_manifest
import doctor
import pytest


def _vault(root: Path) -> list[Path]:
    sources = {
        'knowledge/notes/current.md': '---\ntype: concept\n---\n# Current\n',
        'knowledge/projects/example/state.md': '# Project state\n',
        'knowledge/projects/example/context.md': '# Project context\n',
    }
    ignored = {
        'knowledge/projects/example/journal.md': 'x' * 4096,
        'knowledge/projects/README.md': 'y' * 4096,
    }
    for relative, text in (sources | ignored).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return [root / relative for relative in sources]


def test_journals_and_project_readmes_do_not_inflate_the_claim_count(tmp_path):
    sources = _vault(tmp_path)
    (tmp_path / 'llm-wiki.toml').write_text('[claims]\nmax_pages = 3\n')
    near = doctor._settings_check(tmp_path)['details']['near_ceiling']
    assert near['claims.max_pages'] == {'used': len(sources), 'ceiling': 3, 'share': 1.0}


def test_forecast_bytes_match_the_actual_readonly_claim_snapshot(tmp_path):
    sources = _vault(tmp_path)
    total = sum(path.stat().st_size for path in sources)
    (tmp_path / 'llm-wiki.toml').write_text(f'[claims]\nmax_total_bytes = {total}\n')
    manifest, contents = claim_tree_manifest._snapshot_claim_tree(tmp_path)
    assert len(manifest['entries']) == len(sources)
    assert sum(len(content) for content in contents.values()) == total
    near = doctor._settings_check(tmp_path)['details']['near_ceiling']
    assert near['claims.max_total_bytes'] == {'used': total, 'ceiling': total, 'share': 1.0}


def test_health_reports_overflow_without_bypassing_the_claim_reader(tmp_path):
    _vault(tmp_path)
    (tmp_path / 'llm-wiki.toml').write_text('[claims]\nmax_pages = 2\n')
    near = doctor._settings_check(tmp_path)['details']['near_ceiling']
    assert near['claims.max_pages'] == {'used': 3, 'ceiling': 2, 'share': 1.5}
    with pytest.raises(ValueError, match='claim tree exceeds the page limit'):
        claim_tree_manifest._snapshot_claim_tree(tmp_path)
