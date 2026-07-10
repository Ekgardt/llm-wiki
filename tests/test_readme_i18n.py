"""Guard: public READMEs stay in sync on critical facts.

Prevents shipping EN updates while RU/ZH lag (release regression).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README_FILES = [
    ROOT / "README.md",
    ROOT / "README.ru.md",
    ROOT / "README.zh-CN.md",
]


def _collect_test_count() -> int:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    # last line like "171 tests collected in 0.12s" or "171 selected"
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+)\s+tests?\s+collected", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s+selected", text)
    if m:
        return int(m.group(1))
    raise AssertionError(f"could not parse pytest collect count:\n{text[-500:]}")


def test_all_readmes_exist():
    for p in README_FILES:
        assert p.is_file(), f"missing {p.name}"


def test_all_readmes_share_live_test_count():
    """Every language README must state the same live pytest count."""
    live = _collect_test_count()
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        # badge or prose: 171
        assert re.search(rf"\b{live}\b", text), (
            f"{p.name} must mention live test count {live} "
            f"(update i18n READMEs before release)"
        )
        # ban known stale counts when suite is larger
        for stale in (106, 155, 160):
            if stale == live:
                continue
            if stale < live:
                # allow historical "was 106" only in CHANGELOG, not README badge
                assert f"tests-{stale}" not in text, (
                    f"{p.name} still has badge/tests-{stale}; should be {live}"
                )


def test_all_readmes_use_correct_github_repo():
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        assert "Ekgardt/llm-wiki" in text, f"{p.name}: missing Ekgardt/llm-wiki"
        assert "llm-knowledge/notes" not in text, f"{p.name}: stale llm-knowledge URL"


def test_all_readmes_mention_knowledge_layout():
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        assert "knowledge/" in text, f"{p.name}: must document knowledge/ layout"


def test_all_readmes_mention_current_version():
    """Every README must mention the version declared in pyproject.toml.
    The version is read live so bumping pyproject + READMEs in the same
    change keeps this test green without editing the test itself.
    """
    import re as _re

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    m = _re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), _re.MULTILINE)
    assert m, "could not parse version from pyproject.toml"
    current = m.group(1)
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        assert current in text, (
            f"{p.name}: must mention version {current} (current release per pyproject.toml)"
        )
