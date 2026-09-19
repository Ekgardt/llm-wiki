"""On the stand a turn is found under its keys, because it is keyed before the build.

Third audit, 2026-09-17: the stand built the generation and keyed the turns
afterwards, so the `keys` column it meant to measure was always empty. See
`docs/research/2026-09-17-the-stand-keys-the-turns-before-it-builds.md`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for folder in (REPO / "scripts", REPO / "benchmark"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import longmemeval_vault  # noqa: E402
import search_memory  # noqa: E402
from generation_catalog import GenerationCatalog  # noqa: E402

DAILY = "knowledge/daily/2023-05-26.md"
BODY = (
    "# 2023-05-26\n\n## [10:00:00] session_end | s\n\n_captured: 2023-05-26 10:00:00_\n\n"
    "**user:** I finally saw the production last night and loved it.\n\n"
    "**assistant:** Glad to hear it.\n"
)


def _ask(prompt: str, system_prompt: str) -> str:
    return json.dumps({"0": ["The user attended The Glass Menagerie"]})


def _keys_column(state: Path) -> list[str]:
    catalog = GenerationCatalog(state)
    generation = catalog.generations_path / catalog.get_active()["generation_id"]
    artifact = generation / search_memory.GENERATION_FTS_ARTIFACT
    with sqlite3.connect(f"file:{artifact}?mode=ro", uri=True) as database:
        return [row[0] for row in database.execute("SELECT keys FROM chunks WHERE keys != ''")]


def test_the_generation_the_stand_builds_carries_the_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(longmemeval_vault.FACT_KEYS_ENV, "1")
    root, state = tmp_path / "vault", tmp_path / "state"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / DAILY).write_text(BODY, encoding="utf-8")

    _snapshot, info = longmemeval_vault.build_generation(root, state, [DAILY], ask=_ask)

    assert (info["keyed_turns"], _keys_column(state)) == (1, ["The user attended The Glass Menagerie"])
