# Code classification and repository refusal packets

Date: 2026-11-03. Prepared after daf01519 under the approved decomposition mandate.
These are two already isolated production owners, not a new combined subsystem.
Production modules and public import paths remain byte-identical.

## Responsibilities and alternatives

code_languages owns the immutable suffix map, its version/digest identity and
case-insensitive final-suffix lookup. Corpus collection and code-graph delegation
remain integration consumers. Move only the two pure complete-map/identity tests
from test_corpus_snapshot, preserving their AST. Move the shared independent
expected map into test support so collector parameterization and owner tests use
one fixture. Mixed integration assertions remain in place. Add pure path-flavor,
unsupported/hidden/trailing suffix and immutability checks plus import guards.
A suffix classifier is intentionally not content detection or the LSP registry.
Installing Linguist or merging registries would change supported behavior.

repository_refusal owns the single importable exception class and existing
refusal envelope. Preserve constructor, schema, details precedence and public
identity. Keep CLI/main-module identity regression as integration coverage; it
must not import repository_index into the small unit packet. Add direct envelope
and identity tests with a standard-library-only boundary. Do not introduce a
second exception or move it back into an executable script.

No new limit, dependency, schema or public command. Complete source/test/contract
packets will be measured and round-tripped. Read-only built-in graph must be
refreshed before test changes. Preserve all existing assertions, run consumer
regression, actual complexity and Python 3.10/3.12 qualification. No deployment.

## Current primary sources and tradeoffs

Checked 2026-11-03, four independent publishers across both decisions:
- Python pathlib documents pure lexical paths and final suffix; Python __main__
  explains separate execution identity. Current docs and 3.10 docs were checked.
  The 3.10 page now says unsupported upstream; this change preserves the project's
  >=3.10 compatibility contract and does not claim upstream maintenance.
  https://docs.python.org/3/library/pathlib.html
  https://docs.python.org/3.10/library/pathlib.html
  https://docs.python.org/3/library/__main__.html
- GitHub Linguist documents an ordered chain including modelines, filenames,
  shebangs, suffixes and heuristics. This is an alternative with different
  behavior and dependencies, not evidence that suffix lookup is comprehensive.
  https://raw.githubusercontent.com/github-linguist/linguist/main/docs/how-linguist-works.md
- Import Linter's forbidden/independence contracts motivate enforceable import
  boundaries. Existing subprocess guards are reused; no dependency is installed.
  https://import-linter.readthedocs.io/en/stable/contract_types/
- pytest documents exception assertions and match handling. Direct tests check
  stable attributes/envelope; CLI regressions continue to prove executable
  identity and named failures, which a unit assertion alone cannot establish.
  https://docs.pytest.org/en/stable/how-to/assert.html

The existing refusal rationale remains authoritative historical evidence:
docs/research/2026-09-26-a-refusal-class-lives-outside-the-script.md.

## Implemented evidence

Built-in generation-18e46f1d757895e9-4bba8a99 at 1214d765, fresh caller graph in
logs/code-leaves-graph.json with source fallback inspection. Both production
files byte-identical. Two moved test bodies and all remaining corpus test bodies
are AST-identical; one independent map fixture is shared, not copied. Baseline
consumer suite 241 passed/3 skipped. Initial three-owner plus consumers 298/3;
final Python 3.10 with all boundary/budget/surrogate cases 308/3. Generation
maintenance and slow-worktree consumers 58 passed. Ruff and actual CCN/shape
passed for production and tests. These owners were already independent, so no
production-failure/red-to-green claim is made for their import guards.
Initial complete packets: code languages 71612, refusal 71283 tokens; all 59
packets round-trip every path/byte, largest 97405. Figures precede this evidence
addition. Registry ownership 462/3192, leaving 2730 files unassigned. Full
1214d765 remains running and predates these test-only moves; no live deployment.
Final focused owners: 60 passed. Global ownership/source/guide/bootstrap/structure/
privacy/complexity/test-weight guards: 1445 passed, two skipped, 146.18 seconds.
The final artifact rebuild includes these evidence additions.
