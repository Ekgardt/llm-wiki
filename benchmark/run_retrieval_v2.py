"""Run the frozen public synthetic retrieval-v2 benchmark.

The effectiveness metrics follow the TREC definitions documented by
``ir-measures``: Recall@k is the fraction of all known relevant candidates
retrieved (not Success@k), MRR@10 is the reciprocal rank of the first relevant
candidate, and nDCG@10 uses graded gains with log2 discount normalized against
the ideal ranking. See https://ir-measur.es/en/latest/measures.html.

The adapters in this module are deterministic test machinery. Their fixed
multilingual aliases and hash vectors validate orchestration only; every report
therefore says ``quality_claim: false`` and
``adapter_kind: deterministic-fake``. They are not model-quality evidence.

The implementation lives in the ``retrieval_v2_*`` concern modules beside this
file. This file stays the command-line entry point and the worker script the
selection orchestrator launches by its path.
"""
from __future__ import annotations

from retrieval_v2_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
