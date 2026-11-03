# LSP source coordinates and file URIs

`scripts/lsp_positions.py` owns strict conversion between validated source bytes,
LSP coordinates and file URIs. `code_intelligence` supplies normalized position
values; both full sources and their owner guides are in this packet. Implementation,
import path, original tests and shared Unicode fixture remain byte-identical.

The source document owns line starts and encoding boundaries. UTF-8 byte offsets,
UTF-16 code units and UTF-32 coordinates cannot be substituted for one another.
Invalid codepoint/surrogate splits and reversed ranges retain their existing
refusals. The existing bounded line cache retains its locking and eviction policy.
No new limit or runtime configuration is introduced.

URI conversion preserves the current POSIX, Windows drive and UNC rules,
percent-escape validation and device-namespace rejection. It is a conversion,
not a repository-containment permission; downstream owners must validate access.
The complete original test_lsp_positions.py and its unicode_api.py fixture are
included. The fixture is shared, not copied; other consumers retain their paths.
Run `pytest tests/modules/lsp_positions tests/test_lsp_positions.py -q`.

Only code_intelligence is permitted by the isolated product-import guard.
Instruction, bootstrap, measurement and source/guide closure checks apply.
Cross-platform pure path cases are not native Windows/macOS execution evidence.

## Structural reference retained from the overview

### LSP source positions (qualified in place)

`scripts/lsp_positions.py` remains the owner of source-byte/UTF coordinate and
file-URI conversion; `code_intelligence` owns the value contracts it consumes.
URI conversion does not grant repository containment. Its source, tests and shared
Unicode fixture remain in place. See [research](../research/2026-11-03-lsp-positions-packet.md).

Full owner contracts: [Interruption](interruption.md), [LSP positions](lsp-positions.md).
