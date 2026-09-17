# Impact reads what the diff really holds

Date: 2026-09-17. Audit 3, code intelligence, findings A13, A14, B31, B32 and
the depth half of B33.

Files: `scripts/impact_analysis.py`,
`tests/test_impact_reads_what_the_diff_really_holds.py`

## What was found

1. **A13, appended lines.** A pure insertion has an empty old range, so
   `_overlaps` falls back to a line test on old line `prefix + 1` — the line
   *after* the insertion point. Reproduced: a line appended to the end of `f`,
   with blank lines before `g`, matches nobody and `changed_symbols` is empty;
   with no blank line it is attributed to `g`. The 2026-09-11 decision
   (`docs/research/2026-09-11-a-grown-line-does-not-reach-the-next-symbol.md`)
   says the fallback "names the symbol containing the insertion point"; an
   insertion point lies *between* two old lines, and the code only ever looked
   at the second.
2. **A14, large graphs.** `_affected_nodes` reads every edge of seven types with
   `max_rows=10_000` and no anchor. `EvidenceGraph._execute` refuses above the
   ceiling, `_map_graph` turns the refusal into a warning, and `affected` stays
   empty. `evidence_graph.py` records 35,313 resolved `CALLS` assertions in this
   repository's live generation, so on the owner's vault the refusal is the
   normal case, not the rare one. (Not re-measured here: the fix agent does not
   open the live vault.) `edges()` already takes `target_node_ids`.
3. **B33.** `_extend_reach` adds to `reached` while it is still scanning the same
   edge list, so one "round" can walk several hops and `max_depth` is not a
   depth.
4. **B31.** A submodule bump is a raw record with mode `160000` and a *commit*
   oid. It is sent to `git cat-file blob`, which fails, the `ValueError` aborts
   `collect_git_changes`, and the whole diff — ordinary files included — becomes
   `changes = []`.
5. **B32.** A changed symlink (mode `120000`) read from the worktree raises
   `PermissionError` out of `read_stable_bytes`; `collect` catches only
   `ValueError`, so the error escapes through `mcp_server._analyze_impact`.

## Sources

- Git documentation, `git-diff` "RAW OUTPUT FORMAT"
  (https://git-scm.com/docs/git-diff#_raw_output_format): each record is
  ":" + mode for "src", mode for "dst", sha1 for "src", sha1 for "dst", status.
  The modes are already in the header this module parses; it kept only the
  oids and the status.
- Git documentation, `gitsubmodules` / `git ls-tree`: a gitlink entry has mode
  `160000` and type `commit`, so there is no blob to read.
- The repository's own reader contract: `EvidenceGraph.edges` "anchor the
  question in SQL so a node-scoped query reads its own handful of rows instead
  of the whole edge set", and `impact_symbols.py` already walks with a bounded,
  anchored reader.

## Alternatives

- For A13: attribute an insertion to both neighbours (over-reports, which M13
  was written against); or decide by the inserted text. An indented first line
  continues the block above; anything else starts what follows.
- For A14: raise the ceiling (moves the cliff, the reader belongs to another
  area); or walk breadth-first from the changed symbols with anchored,
  512-id-sliced reads — each round reads only the edges that point at the
  previous round's nodes.
- For B31/B32: catch more exception types (hides the class); or stop asking for
  bytes that are not a file's source. Only modes `100644` and `100755` are
  regular files.

## Decision

- The old side of an insertion-only hunk is anchored to old line `prefix` when
  the first non-blank inserted line is indented and there is a line above, and to
  `prefix + 1` otherwise. Its `line_start`/`line_end` carry that anchor, so the
  evidence shows the line that was actually tested.
- `_affected_nodes` walks round by round: a round asks for edges whose target is
  in the frontier (sliced to the reader's 512-id bound), keeps confirmed edges to
  nodes not yet reached, and those nodes are the next frontier. `max_depth` is
  now exactly the number of hops. A slice that alone overflows the row ceiling
  is still refused by name and still becomes the existing warning.
- Raw records keep `old_mode` and `new_mode` (internal, not in the public change
  keys). A side whose mode is not a regular file has no blob: the change is
  still listed, with no ranges, and the rest of the diff is analysed.
