# A short page still needs evidence

Research date: 2026-09-29.

The linter applied two unrelated word thresholds: it rejected every page below
200 words, but required provenance only after 50 words. Thus padding a page
could satisfy one test while a short unsupported claim escaped the other.
Neither threshold follows from the owner's requirements. The operating contract
prefers concise pages and explicitly permits a `Source:` or `Evidence:` line;
the implementation recognized a section heading or frontmatter field instead.
Two immutable decisions already cited their sources in the permitted line form.

The default sparse check should detect an empty body, after removing YAML and
headings, rather than impose a length quota. A caller may still request a word
floor with the existing `--sparse-words` option. Every claim-bearing page should
require provenance regardless of length. Recognize a nonempty source line as
well as the existing heading/frontmatter forms. Do not rewrite accepted decisions
or pad private notes to satisfy a misleading measurement.

Alternatives: retaining the 200-word quota encourages filler; exempting selected
private pages would hide the general defect; asking a model to grade every page
would add cost and nondeterminism to a structural linter. The selected checks
measure body presence and provenance consistently. They do not prove factual
correctness or that every useful detail has been captured; evidence verification
and review retain that responsibility.

Primary sources checked on the research date:

- [Google Technical Writing](https://developers.google.com/tech-writing/one/short-sentences)
  recommends removing unnecessary words while keeping the meaning.
- [Microsoft Style Guide](https://learn.microsoft.com/en-us/style-guide/word-choice/use-simple-words-concise-sentences)
  prioritizes simple words and concise sentences.
- [Diátaxis explanation](https://diataxis.fr/explanation/)
  relates explanatory content to understanding and context, not a universal
  minimum word count.

These sources support concise, purpose-driven documentation. They do not supply
a new numerical quota. One body word is a nonempty-body predicate, not a claim
that one word is a complete explanation. No dependency, directory, environment
variable or runtime protocol changes. Python 3.10 compatibility is retained.

Qualification includes metadata-only pages, concise supported claims, concise
unsupported claims, the accepted inline citation forms, empty citation lines,
and an explicitly requested larger word floor. All existing evidence hash and
claim-schema checks remain required.
