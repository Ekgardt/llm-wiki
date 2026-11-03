# Canonical module contracts beside their owners

2026-11-03; approved full decomposition mandate; baselinef95cb681.
The largest packet is97517 tokens. STRUCTURE has accumulated detailed descriptions
of each extracted module, so every isolated owner repeatedly receives contracts
for unrelated modules. The append-only style also left stale ownership prose,
corrected in the preceding phase. Do not remove necessary context or weaken the
limit. Decompose this documentation by the already approved code responsibilities.

Keep docs/STRUCTURE.md as the canonical entry point: original global architecture,
three-zone/runtime/security contracts, completion mandate, whole-packet rules,
serialization, bootstrap and ownership requirements remain. Keep existing section
anchors/navigation for moved descriptions. Transfer per-module contract blocks
verbatim to existing docs/modules/<owner>.md, with one-time byte-conservation
verification against the baseline. Shared blocks may be retained by each named
owner when their scope is inseparable; no policy is reduced to a summary.

Add an integration guard: every measured product source with a declared owner
requires that owner's full guide in its packet, including lower dependencies.
Use the explicit ownership registry, not filename guessing. Existing required
instructions, source/test/data/bootstrap files and historical research stay in
packets. Only unrelated owners' detailed descriptions leave common STRUCTURE;
the applicable full guides remain or are added. The root retains links and the
unqualified LSP process-tree status. Runtime behavior and source files do not move.

Three independent primary documentation sources checked today:
- Diataxis reference describes authoritative reference arranged around the
  machinery and its structure: https://diataxis.fr/reference/
- Google Technical Writing discusses choosing interconnected documents, outlines
  and navigation: https://developers.google.com/tech-writing/two/large-docs
- Microsoft Style Guide describes discrete components and explicit navigation:
  https://learn.microsoft.com/en-us/style-guide/scannable-content/
These support organization, not a claim that dropping context saves task tokens.
No technology/dependency or compatibility version changes.

Alternatives: keep appending to the global document (growing irrelevant context
and contradictory descriptions), summarize owner policies (loses exact details),
or add a second lossless codec for paragraph copies. A preliminary probe of the
largest packet found no duplicate >30-token paragraphs after whole-file dedup,
so no unmeasured codec complexity is justified. Select documentation ownership
with exact transfer and dependency-guide coverage; measure actual resulting
serialized artifacts before claiming any size improvement. Paired LLM task-cycle
qualification remains an independent unfinished requirement.

Transfer retains the first124456 bytes of global architecture text unchanged.
26 contract blocks are copied in full to42 owner guides; three other owner guides
remain linked from retained global policy/status. Existing section headings and
anchors remain in STRUCTURE with full-contract links. Three stale phrases are
corrected explicitly before transfer: cleanup no longer owns child launch/Pipe or
heartbeat, runtime filesystem owners reach the Windows helpers, and queue_sources
is implemented rather than the next planned boundary. The proof record preserves
old/new strings and hashes of every transferred body. All other transferred text
is unchanged, and every retained global paragraph remains present.

Shared-block refinement: when one existing guide is already included by every
owner named in a shared block, retain the block once there and link from the
other guides. The runtime family block stays in runtime-contracts; native/ACL in
windows-native; defaults/heartbeat in reliability-defaults. Every affected owner packet is verified to contain that full
unchanged block through the already required guide. No new reference-only packet
or inferred omission is introduced. Groups without such a common guide retain
their copies. This removes redundant copies created by the initial conservative
transfer, while preserving all text and each applicable packet's contract.

The guide guard follows explicitly marked shared-canonical references recursively,
with cycle detection and repository containment. Malformed references fail rather
than disappear. Before enrichment45 cases failed and2 passed; after the transfer,
all47 passed; recursive/shared-reference checks bring the focused run to50 passes.
44 packets gained46 previously undeclared guide references, predominantly the
codec guide. No previously declared instruction, source, test or research file was
removed. Necessary shared-guide bodies are present in every applicable packet.

Qualification: 1062 module/integration guards passed, 2 skipped; Python 3.10
ran 51 relevant tests successfully. Ruff and the actual complexity/shape guard
passed. All 45 emitted packets decode to exactly their declared paths and bytes;
the largest is 96013 tokens. Before this final evidence paragraph the overlapping
packet sum was 3562069 serialized versus 3899075 raw tokens. These are packet
measurements, not paired task-cycle savings. Ownership is 351 of 3087 tracked
files; the whole-project decomposition remains incomplete. The pinned c8036cde
full regression completed separately: 15608 passed, 244 skipped, 5 warnings.
No Windows/macOS run or live installation is claimed.
