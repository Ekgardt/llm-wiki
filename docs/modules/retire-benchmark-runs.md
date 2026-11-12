# Retire benchmark run directories nobody has touched for a month

`scripts/retire_benchmark_runs.py` and the tests that exercise them most.

`cache/benchmarks/` holds two kinds of directory: dataset caches, named by the `DATASET_DIR` of each `benchmark/*_data.py`, which are expensive to fetch again and are kept; and run outputs an operator command wrote (`full-2026-09-17`, `locomo-2026-09-19`, with staged vaults inside), which nothing removed: 1.5 GB on 2026-09-24. A run directory not modified for the setting `retention.benchmark_run_days` (30 by default) is removed; `cache/` is disposable by contract, and the results worth keeping are published under `benchmark/*.json`. Run by the nightly pass. See `docs/research/2026-09-24-every-store-has-a-bound.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
