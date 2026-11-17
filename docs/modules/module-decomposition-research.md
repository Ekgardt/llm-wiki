# Module decomposition research

The studies and proposals behind the way this repository is cut into modules:
why a packet may carry a derived description of a provider instead of its source,
how that contract was tried on paired maintenance tasks, when a packet may use
it, why the import graph has no cycle, and why the giant facades became concern
modules.

| Note | What it settles |
|---|---|
| `docs/research/2026-11-03-composition-context-investigation.md` | what complete maintenance context a composition entry point needs |
| `docs/research/2026-11-03-qualified-dependency-contract-proposal.md` | the qualified dependency contract: a description bound by digest to the code |
| `docs/research/2026-11-03-dependency-contract-instrumented-pair.md` | an instrumented repeat of one maintenance task in two verification phases |
| `docs/research/2026-11-03-dependency-contract-paired-maintenance-result.md` | the first paired run: correctness observed, full cost not measured |
| `docs/research/2026-11-04-qualified-contract-rollout-proposal.md` | the conditions under which a packet may use a qualified contract |
| `docs/research/2026-11-09-the-import-graph-has-no-cycle.md` | how the one import cycle across 77 scripts was removed |
| `docs/research/2026-11-10-giant-files-become-concern-modules.md` | how four over-limit files became concern modules behind their old names |

The code these notes describe belongs to `context-tooling`; this module owns only
the records, so a change to the tooling can cite them without carrying them.
