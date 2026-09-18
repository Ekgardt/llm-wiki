# A project slug is a name, not a phrase

Dated 2026-09-18. A false refusal by the privacy guard, found when the second round was
merged into the vault's own checkout.

## What was found

- `tests/test_nothing_private_reaches_the_public_repository.py::test_no_tracked_file_names_another_project`
  takes the owner's other project names from the live vault's `knowledge/projects/` and
  refuses any tracked file naming one. It matched `refusal-names` — a project the vault has
  held since 2026-09-03 — inside four tracked files, in the file name
  `docs/research/2026-09-18-lsp-a-refusal-names-its-component-and-its-rule.md`.
- Nothing private is there: the phrase is "a refusal names its component", split by the
  hyphens a dated note's name always uses. `\b` treats a hyphen as a boundary, so any project
  whose slug is two ordinary words matches ordinary prose written in slug form.
- The guard only fails in the vault's own checkout, where those project directories exist, so
  the branch's own runs were green and the merge was the first to see it.

## Practice on this date

- The test's own rule (written into it) is that the names must never be listed in the tracked
  tree — they are derived from the vault at run time. That is right and stays. What has to
  change is the boundary: a leak names a project as a token — in a path, in quotes, after a
  space — not as two words inside a longer hyphenated phrase.
- Python's `\b` sits between a word character and a non-word character, and `-` is not a word
  character (`re`, "Matches the empty string, but only at the beginning or end of a word"),
  which is exactly why the hyphenated phrase matched.

## The decision

- The boundary becomes "not a letter, digit, underscore or hyphen" on both sides: a slug found
  inside a longer hyphenated phrase is no longer a match, while `project-alpha`,
  `"project-alpha"`, `/project-alpha/` and `project-alpha.md` still are.
- A test for the guard itself pins both halves: a real leak is still refused, and a phrase in a
  dated note's name is not.

Files: `tests/test_nothing_private_reaches_the_public_repository.py`,
`docs/research/2026-09-18-a-project-slug-is-a-name-not-a-phrase.md`.
