# A code generation is not abandoned

Dated 2026-09-15. A regression of mine from 2026-09-14 (`d72873a`, merged in PR 35),
found by the parity stand. The research before the fix.

## What was found

- The parity stand on 2026-09-14 21:10 answered 16 of 16 on both our sides, reading
  generation `generation-18d4b17d30745c65-6e8ae888` (84 525 unresolved edges reported, a
  full code graph). On 2026-09-15 it answered 2 of 16, reading
  `generation-18d560382cd1fa3b-a22027cf` (799): callers empty. The same 2 of 16 came from
  the tree of `5940eb9` without today's retrieval change, so the change is not the cause.
- The nightly pass of 2026-09-15 03:16 logged
  `prune_generations: removed abandoned generation-18d4b17d30745c65-6e8ae888 (396292765 bytes)`.
- `GenerationCatalog.code_generation_for_repository` says why that was wrong: "The vault
  carries two generations of one checkout — memory, which the active pointer names, and
  code, which is only ever registered". A code generation is never activated by design.
  `docs/research/2026-09-12-the-vault-is-a-repository-too.md` decided it.
- `prune_generations.plan_prune` since `d72873a` calls every registered, never activated
  generation untouched for a day "abandoned" and removes it. The research of 2026-09-14
  (`docs/research/2026-09-14-an-abandoned-publication-is-collected.md`) looked at trees and
  ages and did not read the manifests, so it did not see that a never-activated
  generation holding `code_roots` is a live one. My rule-1 check missed the reader
  `code_generation_for_repository`.
- Foreign repositories' code generations have their own collector,
  `repository_retention`, which decides by identity, never by age, keeps the newest two
  per checkout, and skips the vault. The same prune rule could remove a foreign
  generation that collector keeps.
- The same night's repository refresh did not rebuild the vault's code generation, and it
  had not been able to for as long as a memory generation was the newest of the scope.
  The log of 2026-09-15 03:16: adoption `current`, refresh `refused`,
  `repository_has_no_code_roots`. `repository_index._newest_generation_for` returns the
  newest generation of the scope, and for the vault that is the nightly memory generation;
  `_rebuilt` then asks `index_repository` for that manifest's `code_roots`, which are
  empty, and the build is refused. `detect_repository_changes` and `index_repository`'s
  choice of parent use the same function. For a foreign checkout every generation holds
  code, so only the vault is affected. Git itself answers (`ls-files` 52 683 bytes).

## Practice on this date

- A collector removes only what no root reaches, and every kind of reference counts as a
  root: git gc "tries very hard not to delete objects that are referenced anywhere", keeping
  objects reachable from branches, tags, the index, remote-tracking branches, reflogs and
  anything under `refs/*`, and only then applies a grace period
  ([git-gc](https://git-scm.com/docs/git-gc)). Age is the second test, never the first.

## The decision

- A never-activated generation is abandoned only when its manifest holds no code: an
  aborted memory publication. A generation whose manifest carries `code_roots` is read
  without the pointer and is left to the collectors that know its readers
  (`repository_retention` for foreign checkouts). A registration whose manifest cannot be
  read is not abandoned either.
- Everything that works on a repository's code asks for its newest code generation:
  `repository_index._newest_generation_for` returns the newest registration holding
  `code_roots` for the scope (`GenerationCatalog.code_generation_for_repository`, the
  reader code answers already use). Detection, refresh and the parent of a build then
  agree with the answers. With no code generation the vault is `not_indexed`, and the
  timer's adoption builds one without an operator.
- The vault's code generation is rebuilt once now, so code answers stop reading the
  memory generation before tonight's pass.

Files: `scripts/prune_generations.py`, `scripts/repository_index.py`,
`tests/test_a_code_generation_is_not_abandoned.py`,
`docs/research/2026-09-15-a-code-generation-is-not-abandoned.md`.
