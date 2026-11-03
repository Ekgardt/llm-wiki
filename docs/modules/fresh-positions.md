# Current Python definition positions

`fresh_positions.py` corrects existing answer rows from current Python source. It
owns definition line/span discovery, qualified-name fallback and per-answer file
reuse; it never rebuilds a generation. Python AST lines remain one-based. LSP
zero-based positions/encoding and semantic navigation belong to their own owners.

Unknown/unreadable/oversized/invalid files retain the indexed result. Rows beyond
the existing file budget say `line_not_refreshed`; changed lines say
`line_read_from: file`. A known class missing its member must not borrow a namesake
from another class. Module definitions take precedence over nested bare names.
Caller-selected result keys alone are refreshed; unrelated data is preserved.

The existing MAX_FILES/MAX_FILE_BYTES values and their documented limitations
remain unchanged. This is correction for caller-selected paths, not a filesystem
sandbox or general symbol resolver. Dependency: [Python parser](python-parse.md).
All existing unit tests remain at their paths and are owned by this packet;
root navigation integration obligations remain independently visible.
