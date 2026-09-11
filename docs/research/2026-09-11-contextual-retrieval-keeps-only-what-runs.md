# Contextual retrieval keeps only what runs

Date: 2026-09-11. Trigger: the wider Rule-5 debt recorded under audit OPS-15
(`scripts/contextual_retrieval.py`: 10 functions over CCN 5 — `get_context`
25, `_safe_logical_path` 19, `generate_context` 17, `build_all_contexts` 15,
`build_snapshot_contexts` 14, `_legacy_slug` 13, `_validate_llm_options` 12,
`generate_context_for_source` 10, `_generation_model_identity` 8,
`_extractor_version` 6).

## What is there

Every public entry point calls `_reject_contextual_llm(use_llm)` first, which
raises for `use_llm=True` ("unavailable pending frozen ablation"). The code
after it that builds an LLM prompt, validates prompt bounds, calls
`call_llm`/`call_candidate` or chooses `mode: "llm"` for a generation can
therefore never run: `generate_context_for_source` (prompt branch),
`build_snapshot_contexts` (`if use_llm:` validation and the `"llm"` mode),
`generate_context` (the whole LLM tail) and `_validate_llm_options`, whose
only callers are those branches. The tests pin exactly this: opt-ins are
refused (`test_snapshot_context_llm_opt_in_is_rejected_until_ablation`,
`test_source_llm_opt_in_is_rejected_until_ablation`) and the default is
deterministic. The cache-identity code for `generation_mode="llm"`
(`legacy_context_cache_path`, `context_artifact_key`) is reachable and tested,
and stays.

## Sources

1. Owner's instruction for this repository: remove what is unneeded and
   useless without breaking anything.
2. The ablation gate itself (`knowledge/notes/…`, docstring of
   `generate_context`): LLM context is off until a frozen ablation shows it
   helps. When it is turned on, the path is written against that ablation's
   design; the dead copy is in git history (`git log -p -- scripts/contextual_retrieval.py`).
3. The refactor pattern used on every stand this week: one check per
   function, the same messages in the same order.

## Decision

- Delete the unreachable LLM branches and `_validate_llm_options`; keep every
  public signature (the `use_llm`, prompt-bound and model arguments are still
  accepted and still refused or unused), every error message, and the
  LLM cache-identity code.
- Split the rest into named steps: legacy and generation lookups of
  `get_context`, the slug and logical-path predicates, the snapshot
  publication (absent output, staged artifacts, publish), the legacy page
  read, and one outcome per page in `build_all_contexts`.

Files: `scripts/contextual_retrieval.py`, `CHANGELOG.md`.
