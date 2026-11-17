"""Helpers shared by test_an_abandoned_publication_is_collected.py and the test files importing them."""
from __future__ import annotations

import os
import time
from pathlib import Path


def _age(directory: Path) -> None:
    from llm_wiki import generation_lineage

    old = time.time() - generation_lineage.ABANDONED_AFTER_SECONDS - 60
    for path in [*directory.iterdir(), directory]:
        os.utime(path, (old, old))
