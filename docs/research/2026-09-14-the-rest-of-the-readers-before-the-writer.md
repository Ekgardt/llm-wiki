# The rest of the readers before the writer

Dated 2026-09-14. Items 1.3–1.6 of `docs/AUDIT-2026-09-14.md`. The research before
the fix. Items 1.1 and 1.2 (the Claims block, one page's frontmatter) and the
wikilink case were fixed earlier today; this is the same class in the remaining
readers.

## What was reproduced

Each case was built as a minimal input and run through the real extractor into
`evidence_graph.create_generation_database` on a temporary directory; each fails
the whole build, not one record.

- **1.3 A long code signature.** `code_extractor.symbol_identity` joins the full
  signature into the `code-symbol/v1` identity key, unbounded. The writer refuses
  an `identity_key` over 4 096 characters. Reproduced with the real
  `typer/params.py` from `.venv` (its `Option` signature is 23 569 characters):
  `identity_key must be a bounded non-empty string`. A line break in a route path or
  a `__tablename__` gives the same; a lone surrogate in a route path (a Python
  string escape) gives `node metadata must be finite canonical JSON`.
- **1.4 Raw YAML values in the knowledge extractor.** `project:` with several lines
  or over 4 096 characters becomes a node identity key and is refused;
  `status: 2026-09-01`, `type: 1.5` or `status: .nan` reach node metadata as a
  `date` or a non-finite float and are refused as not canonical JSON.
- **1.5 File names and line endings.** A `\n` or `\r` in a note's or code file's
  name gives `source_id must be a bounded non-empty string`; a backslash gives
  `relative_path must use normalized POSIX separators`, or for code `captured source
  path must be canonical`. A relative path over 505 characters overflows the
  512-character `source_id`. A Python file with old Mac line endings (bare `\r`)
  raises `IndexError` in `_python_name_span` or `occurrence line range does not
  match its captured source bytes`: Python's parser counts `\r` as a line break,
  `_line_offsets` counts only `\n`, and the writer counts `\n`.
- **1.6 The legacy tier build.** A page named `aux.md` or `a:b.md`, or a page that is
  not UTF-8, raises outside the per-page `try` in `build_tiers` and stops the whole
  weekly run (`tiers: failed`).

## Practice on this date

- The writer's validation is the contract and stays strict; the readers in front
  of it must produce only what it stores — the principle recorded for the wikilink
  case this morning (`docs/research/2026-09-14-a-link-lives-on-one-line.md`) and for
  one page's frontmatter (`…-one-page-cannot-close-the-vault.md`).
- An identity too long or too irregular to store verbatim is stored as its digest:
  content-addressed identifiers are how Git names objects and how this graph already
  names its nodes (`code_extractor._identifier` hashes when the text is not a safe
  id; `knowledge_extractor._identifier` does the same)
  ([Git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)).
- Python reads source with universal newlines — `\n`, `\r\n` and `\r` all end a
  line — so a byte offset table built from `ast` line numbers must split lines the
  same way ([Python lexical analysis: physical lines](https://docs.python.org/3/reference/lexical_analysis.html#physical-lines)).

## The decision

1. **One storable-identity rule, shared by the writer and its readers.**
   `graph_storable.storable_identity_key(key)` — a standard-library-only module, so
   the code extractor stays importable as a package; `evidence_graph` takes its text
   rule from the same module — returns the key when the writer would
   store it, else `sha256:` and its digest. Both extractors' node funnels
   (`code_extractor._Collector.add_node`, `knowledge_extractor._node`) use it. The
   node id is still derived from the original key, so identities stay distinct and
   stable.
2. **Node metadata is made storable at the funnel.**
   `graph_storable.storable_metadata(value)` turns dates into ISO text, drops
   non-finite floats, and replaces lone surrogates, recursively; both funnels apply
   it.
3. **A path the graph cannot store is not a source.** The corpus collector skips a
   file whose relative path has a control character or a backslash, or would make a
   `source_id` longer than 512 characters, as it already skips a binary file.
4. **Python line offsets follow Python.** `_line_offsets` breaks lines at `\r\n`,
   `\r` and `\n`, and the line numbers written for an occurrence are counted the
   writer's way, by `\n`. For `\n` and `\r\n` files both are identical to today.
5. **One page cannot stop the tier build.** A page that cannot be read as UTF-8 or
   whose path is unsafe counts as `errors` for that page.

Why not the alternatives:

- **Loosen the writer.** Its bounds protect every reader of the generation; widening
  them moves the failure to query time.
- **Catch the error in the build and drop the source.** The source's valid records
  would be lost with the one bad one.
- **Truncate long keys.** Two different signatures sharing a 4 096-character prefix
  would collide; a digest cannot.

Files: `scripts/graph_storable.py`, `scripts/evidence_graph.py`, `scripts/code_extractor.py`,
`scripts/knowledge_extractor.py`, `scripts/corpus_snapshot.py`,
`scripts/build_tiers.py`, `tests/test_the_rest_of_the_readers_before_the_writer.py`,
`docs/research/2026-09-14-the-rest-of-the-readers-before-the-writer.md`.
