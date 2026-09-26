# CI runs the real encoder

Date: 2026-09-25. Audit item B-28 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and run locally)

- The unit suite replaces the ONNX/tokenizer libraries, and the `clean-hybrid` job only imports
  them and runs one search with no weights, so no CI job ever opened the model session.
- The pinned encoder `intfloat/multilingual-e5-small` weighs 470,268,510 bytes; the reranker
  2,271,071,852 bytes.
- Run locally on a clean detached checkout with a fresh state root and the `hybrid` extra:
  `install_models.ensure` on the encoder alone returned `present`; `doctor
  --rebuild-generation --time-budget 600` built a generation in about 18 s with the generation
  check `ok`; a search then reported `signals_used = ('lexical', 'dense')` and returned the one
  probe page.

## Source

- `actions/cache`, https://github.com/actions/cache (fetched 2026-09-25): "This action allows
  caching dependencies and build outputs to improve workflow execution time." and "A repository
  can have up to 10GB of caches. Once the 10GB limit is reached, older caches will be evicted
  based on when the cache was last accessed." v6.1.0 resolves to commit
  `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` (GitHub API, same date).

## Decision

- `clean-hybrid` caches the encoder's hub directory (key from `scripts/embedding_model.py`, so
  a new pin fetches anew), fetches the encoder only, writes one probe page, builds a generation
  with `--rebuild-generation`, requires the generation check `ok` and a rebuild recorded, and
  requires a search to answer with the dense signal. The reranker stays out of CI because of its
  size; its session remains unexercised in CI (stated, not hidden).
- `tests/test_ci_policy.py` allows the new action at its full SHA.

## Files

- `.github/workflows/tests.yml`
- `tests/test_ci_policy.py`
- `CHANGELOG.md`
