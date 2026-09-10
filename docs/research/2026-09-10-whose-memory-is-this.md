# Whose memory is this — 2026-09-10

Two findings from the users, one question: what does a fresh install treat
as the user's memory?

**Issue #19.** The repository ships 90 published pages under
`knowledge/notes/`, two example daily logs and the project's own journal. On
a fresh install they are indexed, counted ("89 curated pages" before the user
wrote one) and, worst, replayed as guard rails: a new user's first session
opened with another project's rule about `.gitkeep`. Removing the examples by
hand cut the SessionStart block from 2951 bytes of someone else's rules to
1249 bytes of the user's state.

**Issue #29.2.** The active generation holds 7465 chunks; 609 (8 %) are
`knowledge/`. The other 92 % are `docs/`, `tests/`, `scripts/`,
`benchmark/`: on an installed vault the checkout is the vault, so every
`recall` competes against the product's own source. This is also the first
cause of the RU→EN failure (#29.3).

**What the field does.** Basic Memory and Hermes start a new user with an
empty knowledge tree and an onboarding skill, not sample notes
(https://docs.basicmemory.com/integrations/skills,
https://hermesatlas.com/guide/memory/). Obsidian ships its help as a
separate vault the user opens on purpose. Karpathy's llm-wiki pattern keeps
the wiki the user's own from the first page
(https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

**What is true here and why it is delicate.** For the owner, this directory
is both the source and the live vault (single-directory decision,
2026-08-21): the published decision pages *are* the owner's memory, and
excluding them for everyone would blind the owner's own recall. Code and
docs are in the corpus on purpose: `get_architecture`, the navigation
facade and the LongMemEval stand read the same generation.

**Proposal (needs the owner's yes; structure and defaults change).**

1. *Mark, do not move.* Every tracked page under `knowledge/` gets
   `published_example: true` in its frontmatter (one migration, 96 files).
   Moving them would break the nightly fast-forward for every user.
2. *A fresh vault hides them.* `install.sh` on a vault with no untracked
   pages writes `run/install/knowledge-examples.json` `{"mode": "hidden"}`;
   `--keep-examples` writes `keep`. The owner's vault has no file and keeps
   everything. Guard rails, `lookup_mode`, the SessionStart inventory and the
   default `recall` skip marked pages when the mode is `hidden`. Nothing is
   deleted; `git` and the docs still show the examples.
3. *Default recall reads `knowledge/`.* The corpus is built as today; the
   MCP `recall` and CLI `search` filter to `knowledge/` at query time unless
   the caller passes `scope: "checkout"`. `get_architecture` and the code
   tools are unchanged. The stand builds its own vault and is unaffected.
4. README Quick Start states both.

**What was rejected.** `knowledge/examples/` (a move; breaks updates and the
allowlist), deleting examples at install (loses the published decisions the
docs link to), and indexing only `knowledge/` (breaks code navigation).

**Status.** Research only. No code until the owner answers.
