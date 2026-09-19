# A hint table is made from the code generation

Dated 2026-09-17. Third audit, finding G-M2. The research before the fix.

Files: `scripts/code_hints.py`,
`tests/test_the_vault_hint_table_is_made_from_its_code_generation.py`.

## What was found

- `code_hints.export_hints` opens `EvidenceGraph.open_active_for_repository`. On the vault the
  active pointer names the memory generation of the same checkout, which holds no symbols.
- Reproduced on a temp vault (an active memory generation, then a code generation of the
  same checkout): the hint file's meta names the memory generation and `symbols` is `0`.
- `repository_index._current_hints` compares the file's generation id with the code
  generation id, so the two never match and the table is exported again on every refresh.
- The third part of the finding — retention removing the vault's table as an orphan every
  night — was closed with G-H1
  (`docs/research/2026-09-17-the-vault-code-generations-have-a-collector.md`).
- For every other checkout the two openers reach the same generation: nothing is ever
  activated for it, and both fall to its newest registration.

## Practice on this date

- The product's own rule, verbatim from `EvidenceGraph.open_code_for_repository`: "A code
  generation is registered and never activated, so the active pointer cannot name it — and
  on the vault the pointer names the memory generation of the very same checkout."
  (decision `docs/research/2026-09-12-the-vault-is-a-repository-too.md`). Every code reader
  was moved to that opener on 2026-09-12 and 2026-09-15 (`code_graph`,
  `repository_index._newest_generation_for`); the hint export was missed.
- No outside source is needed or claimed: this is one reader left behind by a decision the
  product already made, and the source quoted is the code that states it.

## The decision

- `export_hints` opens `EvidenceGraph.open_code_for_repository`. No code generation means
  `skipped / no_generation`, as before. Nothing else changes.
- Not done here: `impact_analysis` has the same shape (audit M2, last paragraph) but belongs
  to another area and was only read, not run.
