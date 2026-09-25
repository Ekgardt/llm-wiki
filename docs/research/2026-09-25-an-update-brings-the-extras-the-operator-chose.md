# An update brings the extras the operator chose

Date: 2026-09-25. Audit items A-6, A-7 and A-8 of `docs/AUDIT-2026-09-25-full.md`
(A-6 and A-7 are consequences of the encoder change of the same day).

## Question

Three defects meet in one place: which optional parts a vault has, and who keeps
them current.

- A-6: `hybrid` stopped pulling `torch` when the encoder moved to ONNX Runtime, so
  the reranker, documented as on by default for hybrid, silently turned off.
- A-7: `self_update` compares raw distribution names (`huggingface_hub` installed,
  `huggingface-hub` declared) and does not expand `llm-wiki[semantic]` inside
  `hybrid`, so it never sees `semantic` installed and reads `hybrid` as `{numpy}`.
  It only reports extras; the baseline sync names none of them, so a package newly
  added to an extra (`onnxruntime` in `semantic`) never reaches an installed vault.
- A-8: on a base install the nightly `models` step exits 2 ("library not
  installed") and the pass records a failed night every night.

## Sources

- Python Packaging User Guide, "Names and normalization" (fetched 2026-09-25,
  https://packaging.python.org/en/latest/specifications/name-normalization/):
  names compare after lowercasing and replacing runs of `.`, `-`, `_` with `-`.
- uv, "Syncing the environment" (fetched 2026-09-25,
  https://docs.astral.sh/uv/concepts/projects/sync/): `uv sync` is exact unless
  `--inexact`, and syncs no extra unless `--extra` names it.
- `docs/research/2026-09-10-cross-lingual-memory-world-practice.md` (owner's
  decision of 2026-09-10): the reranker takes a Russian question over English pages
  from MRR 0.60 to 0.98 and is on by default.

## Findings (facts)

1. `pyproject.toml`: `hybrid = ["numpy…", "llm-wiki[semantic]"]`, `semantic` has no
   `torch`; `reranker = ["tokenizers", "torch", "transformers"]`.
2. `self_update._installed_distributions` uses `metadata["Name"]` verbatim; the
   live environment reports `huggingface_hub`. `_declared_extras` removes the
   project's own name, so `llm-wiki[semantic]` contributes nothing to `hybrid`.
3. `BASELINE_SYNC_COMMAND` is `uv sync --locked --inexact --no-default-groups`, no
   `--extra`; `_stale_extras` only names extras in the update report.
4. `install_models.py` exits 2 without `huggingface_hub`; `maintenance_helpers`
   counts any non-zero exit as a failed step. It also fetches the 2.27 GB reranker
   whenever `huggingface_hub` exists, whether or not `torch` is there to load it.
5. Nothing records which extras an operator chose.

## Decision (conclusion)

- **`hybrid` includes the reranker again**: `hybrid = [numpy, llm-wiki[semantic,
  reranker]]`. `semantic` alone stays without `torch`. The documented default holds.
- **The update keeps the extras the operator chose.** An extra counts as chosen
  when at least one distribution only it brings is installed (names normalized,
  self-references expanded, base and dev-group distributions excluded). The update
  syncs `--extra` for each chosen extra in the same inexact call as the baseline, so
  a package added to a chosen extra arrives with the update.
- **Weights follow the runtime.** A pinned model is wanted only when the runtime
  that loads it is installed (`onnxruntime` for the encoder, `torch` and
  `transformers` for the reranker). With nothing wanted, `install_models` has
  nothing to fetch and exits 0; doctor's `models` check asks only for wanted models.

## Edited files

- `pyproject.toml`, `uv.lock`, `.github/workflows/tests.yml` (the hybrid job no
  longer asserts that `torch` is absent; the semantic job does)
- `scripts/self_update.py`, `scripts/install_models.py`, `scripts/doctor.py`
- tests of the three

## Uncertainty

An extra chosen once and then deliberately removed by the operator is re-installed
only if one of its own distributions is still present; removing it fully removes
the signal too.
