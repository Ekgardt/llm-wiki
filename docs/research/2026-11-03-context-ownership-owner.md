# File-ownership inventory is a separate maintenance owner

Date: 2026-11-03. Internal refinement under the approved whole-project mandate.

The context-tooling packet is 97806 o200k_base tokens at 19d78215. Its ownership
inventory is already a separate implementation, tests/module_ownership.py, with
an existing independent synthetic-repository suite in tests/test_module_ownership.py.
Its responsibility is distinct from source closure, byte reconstruction and token
measurement: compare declared file/record owners with Git's tracked inventory,
report omissions/duplicates and fail the explicit completion gate until complete.

Qualify that existing implementation and its whole existing test file in place
as context-ownership. Transfer their declared ownership and the original
2026-11-03-module-file-ownership.md research from context-tooling. Preserve source
and test bytes, CLI/import paths, integration checks and every failure. Include
full measurement and record-ownership dependencies in the new packet. Remove
these files from the old packet only after confirming no remaining own test or
implementation imports/reads them; ownership and shared context are distinct.
All whole-checkout tests remain visible in test_module_registry_integration.py;
this change does not make their unassigned integration obligation disappear.

Alternatives: truncate the canonical instructions or source dependencies; raise
the user's limit; delete historical research; compress away useful context. Those
would violate the contract. A real existing responsibility can own its own complete
maintenance packet without moving code or inventing a wrapper. This is preventive
margin, not a claim that the previous qualified packet exceeded its limit.

Qualification: fresh builtin graph plus direct import/read checks; exact source
and test byte hashes before/after; unchanged synthetic Git/CLI tests; explicit
import contract; global ownership/source/guide/integration checks and every packet's
exact roundtrip/token count. No production behavior, install, dependency or Git
command semantics change. Full AGENTS/CLAUDE/laws/STRUCTURE stay in every packet.

Three independent primary publishers checked 2026-11-03:
- Git project, git-ls-files: https://git-scm.com/docs/git-ls-files — --cached
  inventory and NUL-separated output remain the existing authoritative input.
- Bazel, dependency management: https://bazel.build/basics/dependencies —
  module granularity has costs; retain real data/source dependencies, not only imports.
- pytest, integration practices:
  https://docs.pytest.org/en/stable/explanation/goodpractices.html — import and
  test layout matter; qualifying in place avoids unnecessary path changes.
