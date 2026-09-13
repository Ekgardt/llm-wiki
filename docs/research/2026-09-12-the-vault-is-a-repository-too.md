# The vault is a repository too

Dated 2026-09-12. The owner handed me the decision left open by
`docs/research/2026-09-12-the-parity-run-found-the-stand-first.md` — which
generation holds this repository's code — with one instruction: «решение прими на
основе правил 2 и 4». So: the research first, then the choice, then what it
costs.

## What the two options were

- **A.** The repository step stops refusing the vault's own checkout and builds
  it a **code generation beside** the memory one: one directory, two generations,
  one per lifetime.
- **B.** The vault's own generation takes code roots again, and #29.2 ("the
  memory index holds memory only") is narrowed to mean the search corpus rather
  than the graph.

## Research, current practice on this date

1. Per-repository code indexes, built once and updated incrementally per changed
   file, are the standard shape: "a per-repository index that is built once on
   first contact and updated incrementally on subsequent runs via Merkle-tree
   diffs over the working copy, so a source edit invalidates and re-indexes only
   the affected chunks rather than the whole repository"
   ([ckb wiki, Incremental Indexing](https://github.com/nyxCore-Systems/ckb/wiki/Incremental-Indexing)).
2. Separating document indexes from code-intelligence indexes is what makes that
   incrementality possible: "separating document-level indices from code
   intelligence indices enables efficient incremental updates, allowing each
   change to a repository to only require re-indexing the changed files, not a
   whole-repo index"
   ([Code Isn't Memory, arXiv 2606.22417](https://arxiv.org/pdf/2606.22417);
   the same split is why [SCIP](https://scip-code.org/) is a code-only format).
3. Index lifecycle practice outside code intelligence says the same for a
   different reason: one index per data lifecycle, because "each store may have
   its own data lifecycle", and a rebuild is published by building beside the
   live index and swapping the pointer
   ([Elasticsearch index lifecycle](https://help.hcl-software.com/commerce/9.1.0/search/concepts/csdelasticsearchlifecycle.html)).
   That is exactly the catalog-plus-active-pointer design already here.

## The choice: A

By rule 4, item by item:

- **Speed and economy.** Code changes on every commit; memory changes when a
  daily is compiled. Mixed, a knowledge edit re-derives 240 MB of code index and
  a code edit re-derives the memory index — measured sizes from tonight's
  generations. Separate, each rebuild is the size of what actually changed. This
  is the reason the sources give, and it is rule 4's "быстрая, эффективная".
- **Scalability.** A repository generation is already how every other checkout
  and worktree is indexed. Under A the vault is one more repository; under B it
  stays a special case that no second repository can use.
- **Maintainability.** A removes the special case whose own justification had
  gone stale — `repository_is_the_vault` said "its generation is built and
  activated by the nightly pass", which stopped being true in the same commit
  (`780f91b`) that emptied `VAULT_CODE_ROOTS`. Two half-truths with a hole
  between them is the thing to delete, not to document.
- **Reliability.** A keeps #29.2 intact: the memory index still holds memory
  only, so this product's own tests and docs cannot outrank the owner's pages in
  retrieval — the problem that decision was made to fix.
- **Privacy.** A also keeps `knowledge/` out of the code generation entirely.
  Under B the same generation would hold the owner's private pages and this
  repository's code, and every future code-index consumer would carry both.

B wins on nothing except the size of the diff.

`corpus_snapshot.py:46` already says what A says: "Code is indexed per
repository through `repository_index`, the checkout included **when its owner
asks**." The refusal contradicted the contract the module states; the decision
is to make the code match it.

## What changes

1. `repository_index.admit_repository` stops refusing the vault's own checkout.
2. A vault's code roots exclude its memory tree: `knowledge/` belongs to the
   memory generation, is named in the receipt as excluded rather than dropped in
   silence, and an explicit request for it is refused by name. This also removes
   the collector refusal that blocked indexing a vault-shaped repository at all
   (*duplicate corpus source path*, defect three of the 2026-09-12 note): the
   knowledge tree was reached twice because it was both a code root and the
   memory walk.
3. The nightly registers the vault's own checkout before refreshing registered
   repositories, so the promise the old refusal made — that the nightly builds
   it — becomes true, and the owner's standing requirement that everything works
   without them holds.

## What it costs, stated

- One more generation on disk per vault: about 240 MB, the size measured
  tonight, plus its incremental manifest. The pruner keeps the active generation
  and one ancestor, as for every repository.
- The nightly's repository step gets one more repository to look at, which is
  incremental after the first build.
- The first build of it is a full extraction: measured tonight on this
  repository at about nine minutes for 551 sources with `scripts tests benchmark
  integrations` as roots.

Files: `scripts/repository_index.py`, `scripts/scheduled_nightly.py`,
`scripts/corpus_snapshot.py`,
`docs/research/2026-09-12-the-vault-is-a-repository-too.md`.
