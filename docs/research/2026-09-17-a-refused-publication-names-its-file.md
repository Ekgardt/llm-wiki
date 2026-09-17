# A refused publication names its file

Dated 2026-09-17. Finding Q-M16 of the third audit (medium, reproduced). The research
before the fix. The finding has two halves; only the second is fixed here.

Files: scripts/private_vault_backup.py, tests/test_a_refused_publication_names_its_file.py

## What was found

- Half one, scope. `_source_entries` scans the whole vault root and leaves out only the
  top-level `cache`, `logs` and `run`. Since the two directories were merged (2026-08-21)
  the vault root is also the checkout, so an image carries `.git/`, `.venv/`, `scripts/`
  and the rest. Counted on an installed vault on this date: about 40,000 entries under
  `.venv`, 4,000 under `.git`. `publish` refuses the whole image when any destination
  differs, and in a fresh clone `.git/config`, `.git/index` and every tracked file of
  another commit differ. So `publish` cannot land in a real checkout. The refusal itself is
  correct behaviour under the publish contract ("overwrite nothing"); what is wrong is what
  the image holds.
- Half two, the message. `2026-08-25-publishing-a-restored-image-into-an-installed-vault.md`
  promises "a refusal that names the first conflicting path". The error does carry the
  path, but as an absolute destination path, and the command line keeps only details that
  match `[a-z0-9_:-]{1,128}` so that no machine path leaks into its output. The operator
  sees `"details": []` and cannot tell which file stands in the way.

## Practice on this date

- The race half of the refusal rests on exclusive creation, which Python documents for
  `open()` as mode `'x'`: "open for exclusive creation, failing if the file already exists"
  (https://docs.python.org/3/library/functions.html#open). That stays as it is; only the
  name carried by the refusal changes.
- A path inside the image (`vault/knowledge/notes/x.md`) is the same on every machine and
  says nothing about where the vault lives, so it can be printed where an absolute path
  cannot.

## The decision

- `publish_conflict` carries the logical image path of the first conflicting file
  (`vault/...` or `state/...`), for the planned refusal and for the exclusive-create race
  alike. The command line prints a detail of that shape; absolute paths are still dropped.
- Half one is not decided here. What a private-vault backup contains is a contract, and
  leaving `.git` out decides whether unpushed commits are part of "memory". It goes to the
  owner with the options: back up only what Git does not carry (the ignored knowledge and
  `run/`); or keep the wide image and leave `.git`, `.venv` and tool caches out of it.
