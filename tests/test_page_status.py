"""One rule for which pages are still current, shared by every reader."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from corpus_snapshot import collect_corpus  # noqa: E402


def _page(body: str, **metadata: object) -> str:
    fields = "\n".join(f"{key}: {value}" for key, value in metadata.items())
    return f"---\n{fields}\n---\n{body}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")




def test_an_accepted_decision_is_collected_and_a_superseded_one_is_not(tmp_path: Path) -> None:
    """`accepted` is the word an accepted decision carries; it must stay findable."""
    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    _write(
        vault / "knowledge/notes/in-force.md",
        _page("# In force\nThe rule.\n", type="decision", status="accepted"),
    )
    _write(
        vault / "knowledge/notes/replaced.md",
        _page("# Replaced\nHistory.\n", type="decision", status="superseded"),
    )

    collected = {
        source.record.relative_path for source in collect_corpus(vault).sources
    }

    assert "knowledge/notes/in-force.md" in collected
    assert "knowledge/notes/replaced.md" not in collected
