# Names nothing reads

Dated 2026-09-17. Third audit, dead-code list. The research before the deletion.

Files: scripts/bitemporal_claims.py, scripts/code_graph.py, scripts/compile_memory.py,
scripts/doctor.py, scripts/vault_editorial.py, scripts/code_extractor.py,
scripts/compile_cache.py, scripts/claims.py, scripts/corpus_snapshot.py,
scripts/evidence_graph_builder.py, scripts/lsp_server_profile.py,
scripts/project_journal.py, scripts/evidence_graph.py, scripts/install_control.py,
scripts/workspace_revision.py, scripts/lsp_security.py, scripts/evidence_resolver.py,
scripts/search_memory.py, scripts/impact_analysis.py, benchmark/durability_stand.py,
benchmark/factconsolidation_data.py, benchmark/run_conflict_resolution.py,
benchmark/run_retrieval_v2.py

## What was found

- An earlier pass listed functions, aliases, constants and methods that nothing calls.
- Each name was checked again at the current head with a whole-word search over the whole
  tree. A name counts as unread only when the search shows its definition and nothing else;
  history (research notes, plans, the changelog, audit logs, benchmark result files) is not
  a use. A whole-word search also sees a name inside a string, so `getattr`, `monkeypatch`
  and `__all__` spellings are covered.
- The code graph was asked the same question (inbound CALLS, USAGE and CALL_REFERENCE edges,
  tests included) and returned none for the listed functions.
- Helper groups are removed only together with their single reader: a helper whose only
  caller is a removed function is removed with it, and a helper that anything else reads
  stays.
- `INDEX_BULLET_MAX` is already gone at this head and is skipped.

## Practice on this date

- "Vulture finds unused code in Python programs." and the limit of any such search: "Due to
  Python's dynamic nature, static code analyzers like Vulture are likely to miss some dead
  code. Also, code that is only called implicitly may be reported as unused." (vulture
  README, <https://raw.githubusercontent.com/jendrikseipp/vulture/main/README.md>, fetched
  today.)
- The same README on certainty: "Each chunk of dead code is assigned a *confidence value*
  between 60% and 100%, where a value of 100% signals that it is certain that the code
  won't be executed. Values below 100% are *very rough* estimates".
- So a static report alone is not enough for a deletion. The implicit-call risk is answered
  here by the string-inclusive whole-word search, by importing every touched module, and by
  running the tests of every touched module plus the structure and quality guards.

## The decision

- Delete each listed name that the search shows only at its definition. Remove the imports
  the deletion orphans (`ruff check` names them).
- A name any test or product file reads is not deleted; it is reported.
- If the complexity gate refuses a file because of old functions this change does not
  touch, that file's deletions are skipped and reported; nothing is refactored here.
- No behaviour changes, so no new test: the proof is that the existing tests still pass.
