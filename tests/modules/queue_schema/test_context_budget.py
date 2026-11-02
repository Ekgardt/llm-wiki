"""Count the full declared maintenance context, without silently omitting new files."""
import json
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / 'docs/modules/queue-schema-context.json'


def _context_files(manifest):
    files = {ROOT / name for name in manifest['files']}
    for folder in manifest['directories']:
        files.update(path for path in (ROOT / folder).rglob('*') if _source_file(path))
    return sorted(files)


def _source_file(path):
    return path.is_file() and '__pycache__' not in path.parts


def test_complete_queue_schema_context_fits_user_budget():
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    encoder = tiktoken.get_encoding(manifest['encoding'])
    counts = {str(path.relative_to(ROOT)): len(encoder.encode(path.read_text(encoding='utf-8'),
              disallowed_special=())) for path in _context_files(manifest)}
    total = sum(counts.values())
    print(f'queue-schema context: {total} tokens ({manifest["encoding"]}); {len(counts)} files')
    assert manifest['max_tokens'] == 100_000, 'Changing the owner budget requires explicit approval'
    assert total <= manifest['max_tokens'], counts
