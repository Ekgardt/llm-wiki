# An impact names each edit

Date: 2026-09-26. Audit 2026-09-26 B-8 (three of its four parts).

## Facts

- `impact_analysis._changed_ranges` cut one range per file, from the first
  changed line to the last: two edits far apart made every symbol between them
  "changed".
- Called from a subfolder, the analysis used that folder as the root while Git
  names changed paths from the top of the working tree, so `pkg/a.py` was read as
  `<subfolder>/pkg/a.py`.
- `_project_file_ids` read every file node (`find_nodes(kinds=("file",))`) under
  the 10 000-row ceiling and filtered in Python; on a large repository the read
  refused and `affected` was lost.
- Python's difflib (https://docs.python.org/3/library/difflib.html, fetched
  2026-09-26): `get_opcodes()` returns "5-tuples describing how to turn a into b"
  with tags `replace`, `delete`, `insert` and `equal`; by default "if the second
  input sequence is at least 200 items long, items that account for more than 1% of
  it are considered junk", which `autojunk=False` turns off.

## Decision

- Between the common head and tail, `SequenceMatcher(autojunk=False)` splits the
  change into its runs; each non-equal run is its own range, with the existing
  insertion anchoring. Past 20 000 lines the one-range answer is kept.
- `repository_top` asks `git rev-parse --show-toplevel` and both entry points use
  the top as the root; a directory Git cannot place is used as given.
- `EvidenceGraph.find_nodes` takes `values`, matched in SQL against the metadata
  `value`; the changed paths (both slash spellings) are asked for in slices of 400.
- Not done here: a checkout with `core.autocrlf` still classifies every change
  `approximate`, because the indexed bytes (CRLF) and the diff's old blob (LF)
  differ and their offsets do not line up. Making that exact needs the offsets
  mapped between the two forms; it stays open in the audit.

## Files

- `scripts/impact_analysis.py`
- `scripts/evidence_graph.py`
- `tests/test_an_impact_names_each_edit.py`
- `tests/test_impact_analysis.py`
- `CHANGELOG.md`
