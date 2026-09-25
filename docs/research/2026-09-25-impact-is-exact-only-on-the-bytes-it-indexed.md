# Impact is exact only on the bytes it indexed

Date: 2026-09-25. Audit item B-36 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and by test)

- `impact_analysis._map_side` matches each hunk's *old* byte range against the occurrences of the
  active generation (audit M13 chose the old range for this reason). The generation's occurrences
  are offsets into the bytes it indexed. When those are not the diff's old side (the generation
  was built at another commit, or before an uncommitted edit), the ranges land on other lines, and
  every mapped symbol was still classified `exact`.
- The generation stores each source's bytes (`EvidenceGraph.source_by_path`), and the diff record
  carries the old side's bytes (`old_blob`), so the check is exact and needs no git call.

## Source

- git, "Generating patch text with -p", https://git-scm.com/docs/diff-generate-patch (fetched
  2026-09-25), on the chunk header: "`@@@ <from-file-range> <from-file-range> <to-file-range> @@@`".
  A hunk's ranges belong to one named side of the comparison (the from-file for the old range); they
  locate code only in that side's bytes. (A GNU diffutils page on the unified header could not be
  fetched: 429, then a dropped connection.)

## Decision

- A symbol is `exact` only when the generation's bytes for its file equal the diff's old bytes;
  otherwise `approximate`. The overall resolution is `approximate` when any mapped symbol is.
- The tests' fake graph states the bytes it indexed, as the real one does.

## Files

- `scripts/impact_analysis.py`
- `tests/test_impact_analysis.py`
- `tests/test_impact_is_exact_only_on_the_bytes_it_indexed.py`
- `CHANGELOG.md`
