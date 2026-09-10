#!/usr/bin/env python3
"""Run the frozen retrieval-v2 benchmark.

The legacy-60 and current-generated corpora measured BM25 over the pages this
repository used to ship. Since 2026-09-10 the repository ships no memory, so
those gates have nothing to measure and are retired; the frozen synthetic
`retrieval-v2.json` corpus is the retrieval gate. See
`docs/research/2026-09-10-the-repository-ships-no-memory.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RETIRED_FLAGS = frozenset({"--legacy-only"})


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    retired = sorted(RETIRED_FLAGS & set(arguments))
    if retired:
        print(
            f"run_benchmark: {', '.join(retired)} is retired; the repository ships no "
            "memory to measure the legacy gate on.",
            file=sys.stderr,
        )
        return 2
    from run_retrieval_v2 import main as retrieval_v2_main

    return retrieval_v2_main([arg for arg in arguments if arg != "--retrieval-v2"])


if __name__ == "__main__":
    raise SystemExit(main())
