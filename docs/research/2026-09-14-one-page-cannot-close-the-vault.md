# One page cannot close the vault

Dated 2026-09-14. Item 1.2 of `docs/AUDIT-2026-09-14.md`. The research before
the fix.

## What was found

`corpus_snapshot.collect_corpus` is the one reader of what a source is: the
nightly generation build, the freshness check, MCP and every answer go through it.
Reproduced with one note in an otherwise good vault, each of these makes it raise
and return no corpus at all:

| one note's frontmatter | raised |
|---|---|
| `title: Fix: the thing` | `yaml.ScannerError: mapping values are not allowed here` |
| `---` then `---` (empty) | `ValueError: frontmatter must be a mapping` |
| first line `---`, never closed | `ValueError: unterminated YAML frontmatter` |
| `type: [a, b]`, `project: [a, b]` | `ValueError: corpus metadata values must be scalar` |
| `validity: always` | `ValueError: validity metadata must be a mapping` |
| invalid UTF-8 in a `.md` | `UnicodeDecodeError` |

and with `as_of` set, `valid_from: someday` raises from `_parsed_iso`.
`knowledge_extractor._frontmatter` parses the same YAML a second time with its own
regex and raises `invalid knowledge frontmatter` for the same pages, so even a
corpus that got through would stop the generation there. Our own compile can
write such a page: `compile_memory._escape_yaml` lets control characters through,
and `\x07`, `\x1b`, `\x7f` in a title make PyYAML refuse the whole document. For a
registered repository, any `.md` under its code roots counts — a Jekyll page with
`layout: {{ page.x }}`, a README that opens with `---`.

Today all 364 files of the live vault parse; the nightly is one careless line away
from building nothing.

## Practice on this date

- Obsidian, whose note format this vault follows: notes whose YAML is invalid stay
  notes — when properties arrived in 1.4, "most notes with valid YAML frontmatter
  converted to properties, but many with invalid YAML did not", and the broken
  values show as an unknown type until fixed; the common causes named are a pasted
  tab, a missing closing `---`, unquoted values
  ([bad YAML and properties](https://forum.obsidian.md/t/some-issues-with-properties-and-bad-yaml/67216),
  [invalid YAML in frontmatter](https://forum.obsidian.md/t/invalid-yaml-in-frontmatter/21305),
  [broken front matter causes](https://dev.to/terryzhao/i-got-tired-of-broken-yaml-front-matter-in-obsidian-so-i-built-a-no-upload-fixer-5dij)).
  These are exactly the mistakes a person makes by hand.
- The rule this codebase already adopted for claims and plan operations: "a
  malformed claim costs the claim, never the page it was proposed for"
  (`compile_memory._prune_claim_candidates`), and for binary files under a code
  root: "One stray binary under a code root must not be able to fail the whole
  generation" (`corpus_snapshot._decodes_as_utf8`).
- PyYAML's `safe_load` raises `yaml.YAMLError` subclasses for syntax and
  `ReaderError` for disallowed characters; both are catchable and neither leaves
  partial state ([PyYAML documentation](https://pyyaml.org/wiki/PyYAMLDocumentation)).

## The decision

A page with unreadable metadata costs its metadata, never the corpus; a page that
is not text costs itself; both are named.

1. **One frontmatter reader.** `corpus_snapshot.read_frontmatter(content)` returns
   the mapping, where the body starts, and the problem, if any. Unclosed →
   no frontmatter, body from byte 0, problem named. YAML that does not parse, or
   parses to something other than a mapping → empty mapping, body after the
   closing fence, problem named. Empty frontmatter is an empty mapping and no
   problem. `_frontmatter` (the snapshot's) and `knowledge_extractor._frontmatter`
   both use it, so the two readers cannot disagree on a page again.
2. **Metadata values that are not what their field holds are absent, not fatal.**
   A list or mapping in a scalar field, a `validity` that is not a mapping, a
   `valid_from`/`valid_to` that is not an ISO date: the field is `None` and the
   snapshot's defaults apply (type from the path, status `active`). A page whose
   `status: superseded` sits in broken YAML is therefore indexed as active until
   fixed — the cost is stated, and it is smaller than an empty index.
3. **A `.md` that is not UTF-8 is not captured**, exactly as a non-Markdown file that
   is not UTF-8 already is not.
4. **Named.** `corpus_snapshot.frontmatter_problems(content)` lists the YAML problem
   and every ignored field; `lint_memory` gains one check, `unreadable_frontmatter`,
   which the nightly lint already runs and reports. `README.md` counts the checks
   and is updated with it.
5. **Compile cannot write the characters YAML refuses.** `_escape_yaml` drops C0/C1
   control characters other than tab, and the non-characters PyYAML rejects.

Why not the alternatives:

- **Skip the page entirely.** Its body — the durable knowledge — would leave search
  over a typo in its header.
- **Refuse, but only in the build, and let queries degrade.** Every reader would need
  its own fallback; one reader is the point.
- **Repair the YAML automatically.** Guessing the owner's intent in their file; lint
  names the page and the fix is theirs.

Files: `scripts/corpus_snapshot.py`, `scripts/knowledge_extractor.py`,
`scripts/lint_memory.py`, `scripts/compile_memory.py`, `README.md`,
`tests/test_one_page_cannot_close_the_vault.py`,
`docs/research/2026-09-14-one-page-cannot-close-the-vault.md`.
