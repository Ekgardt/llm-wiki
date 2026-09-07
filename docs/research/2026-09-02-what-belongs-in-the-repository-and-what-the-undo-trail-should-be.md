# What belongs in the repository, and what the undo trail should be

Dated 2026-09-02. Two owner objections: derived and personal material must never
reach GitHub, and the undo trail is wasteful work.

## Part one: what is actually in the repository

Checked before arguing. Of 116 knowledge pages, 83 are tracked, and every one of
them is about the product — architecture decisions, working patterns, debugging
notes about this vault. The two tracked daily logs say so in their own first
lines: *"Synthetic public fixture. Real session content was scrubbed before the
public release."*

One real leak, and it is mine, from yesterday: `fix-pip` — the name of another
of the owner's projects — appears twice in
`knowledge/notes/identity-is-a-function-of-its-content-decision.md`, as the
project whose queue had jammed.

So the state is not a catastrophe. It is one careless project name, and a
mechanism that made that possible.

## Why the mechanism is the real problem

The vault's own contract says it plainly:

> What keeps private knowledge out of a public repository is `.gitignore`, not a
> directory boundary. Read that sentence again before you commit anything.

That is a denylist protecting private data, and a denylist **fails open**: a new
page is private only because a pattern happens to match it, and a page becomes
public the moment someone adds an `!` line — or writes a page whose path the
denial does not cover. The one leak above did not even need that: it was private
content written *inside* an allowed page.

The published guidance agrees on both halves. Sensitive material should be kept
out of repositories in the first place, and `.gitignore` does not undo history —
anything once committed stays in the history and has to be treated as exposed.
The recommended shape is a **separate private repository** for private files,
listed in a configuration file, with the public repository holding only what is
meant to be public; tools like GitGhost exist to do exactly that.

## What it should be

Three changes, in increasing order of size:

**1. An allowlist, not a denylist.** Ignore `knowledge/**` entirely and list the
individual published pages. Then a new page is private by default and stays
private unless someone names it. This is a small `.gitignore` inversion and it
converts a fail-open rule into a fail-closed one.

**2. A gate that reads the content, not just the path.** The leak was private
content inside a public page, which no path rule can catch. A pre-commit check
that refuses staged knowledge containing other project slugs, home paths, or the
owner's address would have caught it. We already have the machinery — the DLP
boundary and `secret_redact` — and it is not applied to what gets committed.

**3. Two repositories.** The published pages are a small, curated set that
changes rarely; the vault is large, private and changes hourly. Splitting them
is the shape the sources recommend and it removes the question permanently. It
is also the largest change and needs the owner's decision, because
`docs/STRUCTURE.md` and the single-directory decision of 2026-08-21 both point
the other way.

## Part two: the undo trail, measured

Every write stores a full copy of the file before and a full copy after. On this
vault: 11 369 transactions, 30 849 image files, **4.58 GB**, median 301 KB per
transaction, before and after almost exactly equal in bulk.

The waste is not a matter of opinion. Hashing every image:

```
всего                     4.58 GB
уникальных блоков         2.30 GB   → 50% are exact duplicates
после сжатия уникальных   0.47 GB   → 79% of what remains is compressible
```

**The same history would occupy about half a gigabyte instead of four and a
half.** A tenth of the size, with nothing lost, because the trail stores whole
copies of a journal that grows by one line at a time.

## What the world does instead

Content-addressed, deduplicated, compressed stores. Restic and Borg both split
files into variable-length chunks by content, store a chunk only if it has never
been seen, and make a snapshot a list of references into that chunk store. Borg
reports 10:1 to 20:1 on homogeneous data; a reported real backup went from
12.5 GB to 3.1 GB — 66% from deduplication and 75% from compression. Our own
numbers land in the same place from the other direction.

And the tool is already approved here. The audit-closure contract of 2026-08-15
names **Restic** for encrypted private-vault recovery, with the existing
maintenance fence. So the undo trail is doing, badly, a job the project already
decided to give to a tool that does it properly.

There is a second candidate the project is even closer to: **Git itself**.
Content-addressed, delta-compressed, already a dependency, and the contract
already calls Markdown and Git authoritative. A local-only repository over the
knowledge tree would give unlimited history at pack-file size, with `git
restore` as the undo.

## What I would propose

- **Deduplicate and compress the existing trail.** Same contract, same 30-day
  window, same guarantees; a tenth of the disk. No decision needed beyond
  "yes" — nothing about what is retained changes.
- **Then reconsider whether the after-image is needed at all.** It is half the
  bulk and it exists to detect a corrupt write. A digest would answer that
  question in 32 bytes. This one changes the transaction contract and is the
  owner's call.
- **Longer term, one store instead of two.** Restic is already the approved
  recovery tool and Git is already the authority. Keeping a third, hand-rolled,
  uncompressed copy store beside them is the actual waste the owner pointed at.

## Sources

- [How: To Separate Private from Public — CommonAccord](https://github.com/CommonAccord/Site-Org/wiki/How:-To-Separate-Private-from-Public)
- [GitGhost — private files in a separate repository](https://github.com/decodingchris/gitghost)
- [Storing Code in Public Repositories: A Do's and Don'ts Guide — MoJ](https://cloud-optimisation-and-accountability.justice.gov.uk/documentation/operations-engineering-legacy/operations-engineering-user-guide/github/storing-code-in-public.html)
- [Storing code in private — MoJ Operations Engineering](https://user-guide.operations-engineering.service.justice.gov.uk/documentation/information/storing-code-in-private.html)
- [Restic vs Borg vs Kopia 2026 — Selfhostr](https://selfhostr.com/comparatifs/restic-vs-borg-vs-kopia-2026/)
- [restic vs BorgBackup: Encrypted Deduplicated Backups](https://www.matthewswong.com/en/blog/restic-vs-borg-encrypted-backups/)
- [Restic vs Borg 2026: Speed, Dedup, Restore Times Compared](https://servercrate.net/restic-vs-borg/)
- Our measurement: `run/transactions`, 30 849 images, 2026-09-02
- Our contract: `CLAUDE.md` — the `.gitignore` sentence, and Restic in the
  audit-closure contract
