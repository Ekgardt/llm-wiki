# Navigation tests under the complexity law

Date: 2026-09-12. Trigger: `lizard -C 5 -w tests/` — the law's own tool —
counted 88 functions over the limit in the test tree. The largest single
file was `tests/test_code_navigation.py` with 15: parametrized tests that
select their scenario with an `if/elif/else` ladder over the case name, and
build three constructor slots with three ternaries each.

## Sources

1. Law 5, verbatim: «при появлении более двух if внутреннюю логику
   необходимо выделять в независимые подфункции», and Early Return instead
   of if-else-if. A ladder over a parameter value is exactly that shape.
2. `~/.claude/tools/ccn/bin/lizard` 1.23.0 is the measurement of record on
   this machine (`~/.claude/CLAUDE.md`, «Реализация закона 5»). It counts
   `if`, `for`, boolean operators and conditional expressions, and — unlike
   the managed radon analysis — does not count `assert`, so a test's real
   debt is the lizard number.
3. `docs/research/2026-09-11-two-small-benchmark-files-under-the-gate.md`:
   the remedy already used in this repository — a ladder over case names
   becomes a table of builders, and a loop body becomes one function.

## Decision

Behaviour unchanged: every one of the 272 tests in the file still passes
and still asserts the same facts.

- One runner table, `_navigation_operation`, replaces four copies of the
  "query / resolve_symbol / verify_edge" ladder; `_OPERATION_ALIASES` maps
  each parametrization's own vocabulary onto it.
- `_callback_slots`, `_operation_slots`, `_candidate_slots` and
  `_fenced_operation_slots` replace the ternary triples that handed a
  callback to one constructor seam.
- `_DEADLINE_CROSSING_STAGES` and `_INVALID_RECORD_BUILDERS` replace the
  four-way and six-way ladders over a stage or record name.
- `_write_cache_sources`, `_record_parsed_documents`,
  `_cache_sources_and_documents` and `_publish_consumed_documents` remove
  the setup repeated across six source-document cache tests.
- Compound assertions become one comparison against a named tuple of
  facts: `_location_facts`, `_fenced_outcome`, `_measured_claims`.

- `tests/test_code_navigation_benchmark.py`: the 121-line AST walker over the
  padding fixture (CCN 26) becomes a per-module, per-block pipeline
  (`_padding_module_facts`, `_padding_block_facts`, `_aug_constants`,
  `_reachable_from`); the gold-query checks compare one named tuple per
  location; `_measured_report` builds its performance and resource sections
  through `_performance_section` / `_resources_section` instead of eight
  ternaries; the fake runtime's `query` returns early when nothing answered.

Files: `tests/test_code_navigation.py`, `tests/test_model_policy.py`,
`tests/test_code_navigation_benchmark.py`, `CHANGELOG.md`.
