# What delimits one entry in a daily log

Date: 2026-08-21. Written to decide how `compile_memory.py` should find the
entry a piece of evidence points at, after finding that it cannot find any.

## The observation that started this

A daily log written by the running system today contains **295 entries and zero
`## [HH:MM:SS]` headings**. The evidence contract in `_evidence_block` looks for
a `^## \[HH:MM:SS\]` heading and refuses when it does not find exactly one. So
no compile operation can cite anything the lifecycle capture writes.

Two families of writers exist, and they disagree:

| Writer | What it emits |
|---|---|
| `flush_memory.py`, `session_end_project_tag.py`, `mcp_server.py` | `\n## [HH:MM:SS] <event> \| <session>\n` then content |
| `user_prompt_capture.py`, `post_tool_capture.py` (via `daily_log_append.locked_append_once`) | `\n<!-- llm-wiki-operation:<sha256> -->\n` then one bullet whose text begins `` `[HH:MM:SS] … ` `` |

`compile_memory.py` already knows the second form: `_DAILY_ENTRY_MARKER`
splits an oversized day into parts on exactly that marker. Only the evidence
binder does not.

## The question

Should the consumer become tolerant, or should the contract be stated and both
sides made to obey it?

## What the current sources say

**The tolerant reader** (Fowler; the pattern write-ups collected below) says a
consumer should take what it needs and ignore the rest, so a producer can evolve
without breaking it. Every write-up that recommends it also names its price:
*silent failure*. A tolerant reader that quietly ignores what it does not
understand turns a loud error into a wrong answer, and makes the wrong answer
hard to trace.

That price is unacceptable here. The thing being parsed is the proof that a
compiled claim came from a real line of a real session. A binder that quietly
guesses at a boundary would attribute a quote to the wrong entry, and the
receipt would still say the evidence resolved.

**Thomson's argument against the robustness principle**
(`draft-thomson-postel-was-wrong`, carried on as `draft-iab-protocol-maintenance`)
goes further and is the better fit. Tolerance is the right instrument only when
the specification is frozen and the implementations are strangers. Where the
specification can still be maintained, tolerance is harmful: errors that are
accepted become entrenched, implementations diverge, and complexity accretes —
his *protocol decay hypothesis*. The remedy he proposes is active maintenance:
state the specification accurately, and make implementations maximally strict
about it.

Here both sides are ours, in one repository, released together. There are no
strangers. Nothing is frozen. This is precisely the case where Thomson says not
to reach for tolerance.

## What follows

Not "accept anything that looks like an entry". Instead: **say what an entry is,
name both delimiters the product already writes, and keep the consumer strict
about that statement.**

- An entry starts at a `## [HH:MM:SS]` heading **or** at an
  `<!-- llm-wiki-operation:… -->` marker, and ends where the next entry starts.
- A heading entry declares the timestamp in its heading. A marker entry declares
  it in the first `[HH:MM:SS]` of its first content line — which is where the
  capture writers already put it.
- Evidence names a timestamp. Exactly one entry must declare it. Zero is a
  refusal; two is a refusal. The message does not change.

Everything the contract already guarantees survives unchanged: the quote must
appear exactly once inside that one entry, and it must be a complete line.

One behaviour does get stricter, deliberately. A heading entry used to run to
the next heading, so it swallowed any marker entries that followed it, and a
quote from one of those could be attributed to the heading. Entries now end at
the next delimiter of either kind, so that attribution is no longer possible.

## Sources

- [Tolerant Reader — Java Design Patterns](https://java-design-patterns.com/patterns/tolerant-reader/)
- [Demystified Tolerant Reader — Frank Scheffler](https://medium.com/digitalfrontiers/demystified-tolerant-reader-ca07d6bea602)
- [Tolerant Reader — Cloud Adoption Patterns](https://kgb1001001.github.io/cloudadoptionpatterns/Microservices/Tolerant-Reader/)
- [Using a tolerant reader for web service integrations — Ben Morris](https://www.ben-morris.com/using-a-tolerant-reader-for-web-service-integrations-in-net/)
- [The Harmful Consequences of the Robustness Principle — draft-thomson-postel-was-wrong-03](https://datatracker.ietf.org/doc/html/draft-thomson-postel-was-wrong-03)
- [draft-iab-protocol-maintenance-00](https://datatracker.ietf.org/doc/draft-iab-protocol-maintenance/00/)

## Evidence in this repository

- `scripts/daily_log_append.py::locked_append_once` — writes the marker form.
- `scripts/flush_memory.py`, `scripts/session_end_project_tag.py`,
  `scripts/mcp_server.py` — write the heading form.
- `scripts/compile_memory.py::_DAILY_ENTRY_MARKER` — the marker is already the
  part boundary for oversized days.
- `scripts/compile_memory.py::_evidence_block` — the binder this note is about.

## One asymmetry left standing, deliberately

`_daily_entry_offsets` splits an oversized day into compilable parts, and it
splits **only** on the operation marker. A day made entirely of heading entries
cannot be split at all, however large it grows — the same specification gap seen
from the producer's side.

That is not fixed here, and the reason is not oversight. Part boundaries decide
`DailySnapshot.part_key` and therefore the digest a compile receipt is written
under. Teaching the splitter about headings would move those boundaries, and
every receipt already written for an oversized heading-only day would stop
matching the day it proves. That is a migration, not a parser fix, and it needs
its own decision.

So the entry definition lives in one place, `evidence_resolver.daily_entries`.
When the splitter is migrated it should consult that function rather than grow
another answer.

## What the first attempt missed

Fixing the compile binder alone changed nothing a user could see. The binder
computes a span and then hands it to `EvidenceResolver.resolve_bytes`, which
re-derives the block from the same log with **its own** heading-only copy of the
definition — and `claims.split_blocks` held a third. Three implementations of
one sentence, exactly the decay Thomson describes, in a repository small enough
that nobody had to guess.

The gap survived nine passing tests because every one of them exercised the
parser directly. The test that caught it binds a captured entry end to end
through `_validate_semantic_operation`. The lesson is narrow and worth keeping:
a test that stops at the seam it is meant to close proves nothing about the
path that crosses it.
