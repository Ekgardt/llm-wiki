# SQLite schema comparison text

`llm_wiki.sqlite_schema_text.normalized_schema_sql` compares managed SQL text.
It folds ASCII uppercase and collapses ASCII whitespace outside quoted tokens
and comments. Single/double/backtick quotes, bracket identifiers, doubled quotes,
comment contents and line-comment terminators remain exact. Unicode characters
remain exact. Unterminated protected tokens retain their remainder unchanged.
Only outside-token leading/trailing whitespace is removed. None means empty text.

This is conservative lexical comparison, not arbitrary SQL semantic equivalence
or a parser. Its output is never executed. Preserving double-quoted tokens avoids
changing SQLite's legacy string-literal interpretation. Comments are intentionally
significant. No filesystem, connection, transaction, migration or runtime ownership
is introduced. Existing queue and coordinator helper names delegate here. Their
managed SQL, schema hashes and versions do not change.

Qualification includes the actual SQLite validators rejecting changed constraints
and Unicode identities, plus unchanged managed schemas and lexical edge cases.
Source: [defect and alternatives](../research/2026-11-03-schema-literals-survive-normalization.md).
