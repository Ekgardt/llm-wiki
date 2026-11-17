"""Every production Python file under scripts/, nested packages included.

A scan that globbed only `scripts/*.py` silently stopped seeing code the moment it
moved into `scripts/llm_wiki/`; the invariant scans read this list instead.
"""
from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def script_files() -> list[Path]:
    return sorted(path for path in SCRIPTS.rglob('*.py') if '__pycache__' not in path.parts)


def script_key(path: Path) -> str:
    """The file's path below scripts/, so `llm_wiki/x.py` never collides with `x.py`."""
    return path.relative_to(SCRIPTS).as_posix()


def split_module_files(facade: str, part_prefix: str) -> list[Path]:
    """A facade under scripts/ and every `llm_wiki/<part_prefix>_*.py` part it is composed from.

    Found by name rather than by importing, so a source scan stays cheap and a
    part added later is read without editing the scan.
    """
    parts = sorted((SCRIPTS / 'llm_wiki').glob(f'{part_prefix}_*.py'))
    return [SCRIPTS / f'{facade}.py', *parts]


def split_module_source(facade: str, part_prefix: str) -> str:
    return '\n'.join(path.read_text(encoding='utf-8') for path in split_module_files(facade, part_prefix))
