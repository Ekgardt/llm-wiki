# A verdict worth remembering across processes

Dated 2026-09-12. The owner looked at the graph warm-up and said it looks like a
crutch. It is, and the word is right: warming pays the cost earlier, it does not
remove it. This note is the research for removing it, and the warm-up goes with
the same change.

## What is actually being paid

Cold `get_architecture mode=query` on the installed vault: **2.22 s**, of which
the generation open is nearly all — resolving the repository scope, then
**hashing every artifact of the generation against its manifest** (241 MB
`evidence.sqlite3`, 28 MB `search.sqlite3`, a 27 MB incremental manifest, the
source manifest and the vectors), then the entry seal. Warm: **0.31 s**, because
the validated reader is cached for the life of the process.

So the cost is per *process*, and it is a re-hash of bytes that were already
hashed the last time any process opened the same immutable generation. In this
codebase the hashing happens in `generation_catalog._ArtifactScan._verify` →
`_hash_artifact`, called from `_scan_artifacts` for every artifact of the
manifest.

## Research, current practice on this date

1. **Git's index is exactly this cache, and has been for twenty years.** Rather
   than reading and hashing every file, Git stores the result of `lstat(2)` per
   path — size, mtime, inode — and compares it against a fresh `lstat(2)`; if
   they match, the file is assumed unchanged and its contents are never read.
   That is what makes `git status` fast
   ([racy-git documentation](https://git-scm.com/docs/racy-git/2.5.6.html)).
2. **The known failure mode has a known rule.** A file modified so soon after
   being recorded that its mtime does not change would be trusted wrongly; such
   entries are called **racily clean**. Git's rule: any entry whose mtime is *not
   strictly older* than the index file's own mtime is suspect, and its contents
   are read and hashed anyway
   ([racy-git](https://git-scm.com/docs/racy-git/2.5.6.html)).
3. **Device and inode are filesystem-local, and that is a feature here.** A tree
   copied from another machine, restored from a tarball or a CI cache keeps its
   bytes but not its inode numbers, so the cache simply misses and the expensive
   check runs — fail-safe by construction
   ([Git Rev News 137](https://git.github.io/rev_news/2026/07/31/edition-137/)).
4. **Build systems do the same under another name.** Bazel's local action cache
   compares current action and artifact metadata against prior execution state
   rather than re-deriving, and ccache identifies a hit by hashing the inputs
   rather than by recompiling
   ([The Many Caches of Bazel](https://blog.engflow.com/2024/05/13/the-many-caches-of-bazel/),
   [ccache](https://en.wikipedia.org/wiki/Ccache)).

## The decision

**Remember the verdict, keyed by stat identity, with Git's racily-clean rule; and
delete the warm-up.**

- A small disposable file, `cache/evidence-graph/verified-artifacts.json`, maps
  (generation id, artifact path, device, inode, size, mtime_ns) → the digest that
  was verified.
- An artifact is accepted without hashing only when its stat identity matches an
  entry **and** its `mtime_ns` is strictly older than the cache file's own
  `mtime_ns`. Anything newer, equal, or absent is hashed exactly as today.
- Any unreadable or unparsable cache is an empty cache: the failure mode is a
  slow open, never a trusted lie.
- It lives under `cache/`, which the contract already calls disposable and
  regenerable; deleting it costs one slow open per generation.

By rule 4 this removes the work instead of moving it: every process opens in
~0.3 s — the CLI path and the benchmark's fresh-process-per-call shape
included — and the warm-up thread, 1.71 s of CPU at every server start and wasted
whenever the session asks nothing about code, is deleted rather than left as
decoration.

## What is given up, stated plainly

A generation whose artifacts keep their device, inode, size and mtime is trusted
without re-reading their bytes. Someone able to rewrite 241 MB in place while
preserving all four could pass this check where re-hashing would have caught
them. That is precisely the trade Git makes on every `git status`, with the same
mitigation, on a single-user local vault whose enforcement contract already says
its gates are "a brake against drift and not a control against intent". The
manifest equality against the catalog row, the seal re-check after the open, and
the deep re-derivation at publication and in `doctor` are untouched.

## Two more places, the same rule

After the first measurement the remaining 1.71 s was profiled again, and two of
its three parts were the same waste in other modules:

- `evidence_graph._require_validated_format` hashed 241 MB (0.32 s) **only to key
  the format receipt it already had** — the receipt is keyed by digest, and the
  catalog had just verified that digest for those exact bytes. It now fetches the
  remembered digest and hashes only when the stat moved.
- `search_memory._stored_chunks_match` walked all 3 405 index rows (0.38 s plus
  SQL) to check their order and uniqueness even when no re-derivation was asked
  for. That check belongs with the re-derivation it was written beside, and the
  read path already has the digest.

Measured after both: **1.23 s** cold, from 4.9 s this morning.

What is left is the seal's own read: `_require_sealed_content` hashes the held
descriptors (0.4 s) to prove the bytes right now, after the open. That is the
fence itself rather than a repeat of it, so it stays, and it is the honest floor
for a cold open.

## Bounds

One cache file per state root; at most 512 remembered entries, oldest evicted;
1 MiB ceiling; canonical JSON; atomic replace; written only after a validation
that succeeded. Tests: a match skips the hash, a changed size or mtime does not,
an entry as new as the cache file does not, and a corrupt cache falls back to
hashing.

Files: `scripts/verified_artifacts.py`, `scripts/generation_catalog.py`,
`scripts/mcp_server.py`,
`docs/research/2026-09-12-a-verdict-worth-remembering-across-processes.md`.
