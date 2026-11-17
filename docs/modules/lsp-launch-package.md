# Verified package launch tree

The existing `lsp_launch_package` owns creation, sealing, read-back verification
and cleanup of the package-shaped launch scratch. Its production source stays
unchanged by ownership qualification. Inputs are a verified snapshot descriptor
and path, a PackageLaunch value, expected digest, owner root and caller checkpoint.
Outputs are a LaunchTree or an exception; removal reports its first failure after
attempting all removals. Process ownership, deadlines, profile selection, server
installation and actual execution remain the caller's responsibilities.

The complete bounded_io and lsp_server_profile implementations stay in this
packet, with their own transitive dependencies and design documentation. Importing
this owner must not load sessions, process launch, registry or runtime publication.
Pure tree tests move with unchanged assertions; profile/guard integration stays in
the original integration file. The child file-size regression keeps real kernel
limits and changes only its import path and repository-root depth after relocation.
The failed-directory-construction regression also moves intact. Nothing is dropped
from the full regression suite; the measured shard weights follow the test files.

This is the existing POSIX package strategy. The same-uid substitution window is
not closed, and read-only modes are not an OS sandbox. Pyright's descriptor launch
remains separate. The original launch research is retained whole, including its
dated measurements and runtime verification limitations; those historical findings
are not assertions that every current runtime path was re-audited here. The complete
path-refusal research and short-write correction are retained as maintenance context.
The authored manifest now completes or raises before acceptance (6dc4bbc7), with no
new power-loss durability promise.

## Research — 2026-11-04

Checked independent primary sources:
[Node ESM](https://nodejs.org/api/esm.html),
[Linux open(2)](https://man7.org/linux/man-pages/man2/open.2.html), and
[Python binary IO](https://docs.python.org/3.14/library/io.html).
Module-relative package lookup, descriptor identity and complete binary writes
support keeping this existing boundary. Moving tiny helpers into unrelated IO,
merging process lifecycle into the tree owner, or changing package strategy adds
coupling or changes security behavior without helping this decomposition. The
selected change qualifies ownership in place; it adds no runtime paths or limits.

Qualification requires a full packet <=100000 o200k_base tokens, byte-exact bundle
reconstruction, isolated imports, actual complexity/shape measurement, original
assertions and related integration checks. Linux/macOS native behavior is required;
Windows skips of POSIX-only tests do not establish POSIX behavior.

## Canonical structural contract

[Verified package launch tree](lsp-launch-package.md) owns the existing
`lsp_launch_package` implementation in place. Its complete dependency sources and
security history accompany pure tree tests; process/profile integration remains
with the session caller. Runtime layout and launch strategies are unchanged.
