# How many superseded generations to keep

Dated 2026-08-29. Written before changing `scripts/generation_catalog.py`, per
rule 2. The question: this vault has published 35 immutable evidence-graph
generations since 2026-08-21 and has never removed one. What does current
practice say a system should keep, and on what grounds?

## What was measured here first

On the live vault, 2026-08-29:

- `cache/evidence-graph/generations` holds 35 registered generations, 6.3 GB,
  the oldest from 2026-08-21T00:37Z. Each nightly build adds roughly 180 MB;
  the two most recent trees are 258 MB and 252 MB.
- The catalog names exactly one active generation. 34 of the 35 appear in
  `activation_history`; one (`generation-18d013d1b93fbeea-d67a7657`) was
  registered and never activated.
- `GenerationCatalog.discard_unactivated` is the only removal path in the
  product, and `_generation_in_use` refuses any generation that is active *or*
  has ever been activated. So 34 of 35 cannot be removed by any supported call.
- A full generation build costs 148.4 s wall (907 sources, 668 rebuilt, 239
  reused). The immediately following idle pass returns `status: current` in
  4.25 s. That 35x gap is what the reuse chain buys, and it is the thing a
  retention policy must not break.

Who actually reads a generation, from the code:

- Retrieval and `get_architecture` resolve the **active pointer**
  (`GenerationCatalog.get_active` → `_select_fallback`).
- The incremental rebuild names the **active** generation as its reuse parent
  (`doctor._build_or_refresh_generation`: `parent_id = _active_generation_id(parent)`,
  then `parent_generation_id=_reuse_parent_id(force_rebuild, parent_id)`), and
  reads `incremental-manifest.json` and the vectors out of that one tree
  (`evidence_graph_builder._load_incremental_manifest`, `_vector_reuse_source`).
- `_fallback_order` widens the read set: active, then every id in
  `activation_history` newest-first, then each of their ancestors through
  `parent_generation_id`. `_select_fallback` walks that order and takes the
  first candidate that validates.

So the reuse chain needs **one** generation — the active one. The parent of the
active is not read by reuse at all; it is read only as the first alternative in
`_fallback_order`. A generation two steps back is read by nothing: it is
strictly older than a candidate that is already in the fallback order ahead of
it, and it is not a reuse source for anything.

## What current practice does

**Nix** separates the two questions cleanly. Its collector deletes any store
object not reachable from a *root*, and roots are explicit: symlinks under
`/nix/var/nix/gcroots`, every profile generation, `/run/booted-system`. Depth
("how many old generations do I keep?") is a *separate*, operator-chosen policy
(`nix-collect-garbage --delete-older-than 30d`), applied by first removing the
generation links — i.e. by removing roots — and only then collecting. The
lesson is the shape, not the number: reachability from a declared root is the
safety property; retention depth is a policy laid on top of it, never a
substitute for it.
([Nix manual, Garbage Collection](https://nixos.org/manual/nix/stable/package-management/garbage-collection))

**Delta Lake** gives the reason a retention floor exists at all, and it is not
rollback. `VACUUM` removes files no longer referenced by any table version
inside `delta.deletedFileRetentionDuration`, default **7 days**, and the
documented rationale is concurrent readers and writers: "old snapshots and
uncommitted files can still be in use by concurrent readers or writers", and if
`VACUUM` removes an in-use file "concurrent readers can fail or, worse, tables
can be corrupted". The interval must exceed "the longest running concurrent
transaction". Databricks makes the shorter setting an explicit opt-out of a
safety check rather than a plain parameter.
([Delta Lake table utilities](https://docs.delta.io/delta-utility/),
[Databricks VACUUM](https://docs.databricks.com/aws/en/delta/vacuum))

**rpm-ostree / OSTree** is the closest analogue to this vault's object: an
immutable whole tree, swapped by moving one pointer. It keeps **two**
deployments by default — the current one and the one it replaced — so that
"there will always be a previous deployment, available for rollback", and so an
update can be staged without destabilising the running one. Two is the smallest
number that leaves anything to fall back to.
([rpm-ostree administrator handbook](https://coreos.github.io/rpm-ostree/administrator-handbook/))

**Kubernetes** keeps ten (`revisionHistoryLimit`, default 10 since apps/v1 in
1.9; the older beta default was 2). The stated reason is fast rollback without a
rebuild — the old ReplicaSet is a scaled-to-zero snapshot of the pod template.
The cost is explicitly acknowledged as etcd space and listing noise.
([Kubernetes rollback / revision history](https://oneuptime.com/blog/post/2026-02-09-deployment-rollback-history-revision-limits/view))

**Bazel** (7.4+) is the counter-shape: `--experimental_disk_cache_gc_max_size`
and `--experimental_disk_cache_gc_max_age` bound a disk cache by *size and age*
and collect in the background while the server idles. That works because every
entry there is independently re-derivable and nothing points at a particular
one; there is no active pointer and no reuse parent.
([Bazel remote/disk caching](https://bazel.build/remote/caching))

## What follows for this vault

1. **Reachability first, depth second.** The invariant is Nix's: never delete
   what a root reaches. Here the root is the active pointer, and the edge is
   `parent_generation_id`. Depth is a separate, named policy.

2. **Depth is rpm-ostree's number, not Kubernetes'.** Kubernetes can afford ten
   because a revision is metadata. A generation here is a 180 MB tree of SQLite
   files and vectors; 35 of them are 6.3 GB on a disk that was at 94%. The
   value a retained ancestor delivers — one fallback step — saturates at the
   first one, because `_select_fallback` takes the first candidate that
   validates and a second ancestor is only ever consulted if two consecutive
   generations both fail to validate, which is a corruption story that a
   rebuild answers better than a two-day-old index.
   **Keep the active generation and one ancestor. Keep nothing else.**

3. **The retained ancestor is a reader window, not a rollback.** Delta Lake's
   rationale applies literally: this machine runs resident `mcp_server.py`
   processes (two were up from the previous day when this was measured), and a
   query resolves the active pointer and then opens the tree. Keeping the
   generation that was active until the last activation is what makes that
   window harmless. Measured caveat, stated because it cuts the other way: at
   the moment of measurement neither resident server held a file descriptor
   under `generations/`, so the window is per-call and short, and this argument
   supports one ancestor rather than a time-based floor.

4. **A time-based floor is the wrong instrument here.** Delta's 7 days and
   Bazel's max-age both fit caches whose entries are unreachable-by-default.
   This catalog already knows precisely what is reachable; an age window would
   only add a way to be both wrong and slow. Age is not used.

5. **Registration, history and tree move together.** `_generation_in_use` reads
   `activation_history`, so leaving a history row behind after deleting a tree
   would leave the catalog permanently refusing to clean up a generation that no
   longer exists. All three drop inside one write transaction, so a filesystem
   failure rolls the registration back instead of stranding a row.

6. **Refuse rather than guess.** Nix's collector deletes only what it can prove
   unreachable. A registration with no tree, or a tree with no registration, is
   the residue of an interrupted operation; that is evidence, and the pass
   reports it instead of picking a half to believe.

## Sources

- [Nix Reference Manual — Garbage Collection](https://nixos.org/manual/nix/stable/package-management/garbage-collection)
- [Delta Lake — Table utility commands (VACUUM)](https://docs.delta.io/delta-utility/)
- [Databricks — Remove unused data files with vacuum](https://docs.databricks.com/aws/en/delta/vacuum)
- [rpm-ostree — Client administration handbook](https://coreos.github.io/rpm-ostree/administrator-handbook/)
- [Bazel — Remote/disk caching and disk cache garbage collection](https://bazel.build/remote/caching)
- [Kubernetes deployment rollback history and revision limits](https://oneuptime.com/blog/post/2026-02-09-deployment-rollback-history-revision-limits/view)
