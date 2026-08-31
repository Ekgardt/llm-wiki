---
type: raw-source
status: active
confidence: high
source_authority: ai-derived
---

# One definition of repository identity — 2026-08-29

Research for `NEW-138`, the fourth filed instance of one mistake. The fix
itself restores an existing contract and needs no research; the question that
does is the one the task asked: **can the codebase be made to refuse the shape
instead of being repaired one site at a time?**

## The shape, stated once

`RepositoryScope` is a frozen dataclass with six fields. Five say *which*
repository checkout this is; the sixth, `git_commit`, says *what a generation
was built from*. Comparing all six answers "same repository **and** same
commit". On a vault that commits its own runtime that reads as "almost never",
so every consumer that asked it silently lost its cache:

| # | Site | What it lost |
|---|---|---|
| `NEW-65` | `GenerationCatalog._get_active` | semantic retrieval fell back to the legacy index |
| `NEW-90` | publication root check | a build spanning a commit failed to publish |
| `NEW-111` | the open loop | the catalog handed back a manifest the opener then refused |
| `NEW-138` | `evidence_graph_builder._reuse_config_matches` | record reuse — the win in `283eb3a` |
| `NEW-138` | `doctor._parent_matches_identity` | the idle maintenance pass |

A sixth, unfiled, was structural rather than behavioural: `doctor` kept its own
copy of the identity field list (`_SCOPE_IDENTITY_FIELDS`), so adding a field to
`RepositoryScope.identity()` would have left `doctor` answering the old
question. Folded into `repository_scope.IDENTITY_FIELDS` in the same change.

## Why narrowing `__eq__` is the wrong answer

The obvious move — make `RepositoryScope.__eq__` compare identity only — is
what the value-object literature recommends for exactly this situation: "if the
object must adhere to an identity standard based on a unique key … the
automatic `__eq__` is incorrect", and equality that depends on volatile fields
"can break … caching logic". `git_commit` is the volatile field and a
generation is the cache, so the diagnosis fits.

It is still rejected here, for three reasons, the first decisive:

1. **It cannot reach half the sites.** `NEW-65`, `NEW-90` and `NEW-111`
   compared `RepositoryScope` objects. Both `NEW-138` sites compared
   `as_dict()` records — plain dicts, where the class is not involved at all
   and no change to it is visible. A fix that catches three of five and looks
   complete is worse than none.
2. **One caller genuinely wants all six fields.**
   `generation_catalog._require_publication_scope` checks that the manifest
   being published records the scope the publisher was handed, commit included;
   both sides come from the same build, so it is self-consistency, not
   identity. Narrowing `__eq__` would weaken it silently — the exact failure
   mode being fixed, in the opposite direction.
3. **`__eq__` on a frozen dataclass also drives `__hash__`,** so narrowing it
   changes set and dict membership for a type used as a key.

The alternative of making `__eq__` *raise* was rejected as well: it breaks
`in`, `assertEqual` and repr-based tests for a gain that reason 1 already caps
at three sites out of five.

## What was built instead

Two things, and neither is a new abstraction.

**One definition of identity.** `repository_scope.IDENTITY_FIELDS` is now the
single field list. `RepositoryScope.identity()` reads it, and a new
`record_identity()` reads the same names off the *serialized* form.
`same_repository_record(left, right)` is the serialized twin of the existing
`same_repository()`. It is deliberately tolerant — `doctor` asks it of
manifests it is diagnosing, which may be corrupt, and a diagnostic must answer
rather than raise — but a record that names neither a repository nor a checkout
has no identity and matches nothing, including another record just as damaged.
The old `==` said two empty dicts were the same checkout.

**A guard that fails on the sixth site.** Current practice calls this
lint-as-architecture: encode the decision as a machine-executable rule that
fails the build rather than as documentation, and build the rule from the bug
that already happened. `tests/test_repository_scope_identity_guard.py` walks
`scripts/`, seeds the names that hold a whole scope, grows the alias set
monotonically through assignments, and reports every `==`/`!=` whose operand is
a whole scope. Syntactic on purpose: syntax is the only thing the object shape
and the dict shape have in common.

The same sources warn that a rule with false positives gets switched off, so:
the guard fires on whole records only, never on a field read (`(x.repository_id,
x.checkout_id)` comparisons stay silent — they carry no commit and are correct);
it ships with three tests of itself, two that must fire and one that must not;
and its two allowlist entries each name the question that site asks, because
"is this the same repository" is never a question for `==`.

Verified against the pre-fix code: run over `HEAD`'s `scripts/`, the guard
reports both `NEW-138` sites, at `doctor.py:7005` and
`evidence_graph_builder.py:2076`.

## The fifth instance: measured negative

An AST sweep over all of `scripts/` for whole-scope equality found **no fifth
instance**. After the fix, the only two hits are the two deliberate ones now in
the allowlist. A looser sweep (any comparison touching a scope-derived value)
adds only field-level comparisons — `(repository_id, checkout_id)` pairs in
`evidence_graph`, `evidence_graph_builder`, `workspace_revision` and
`code_navigation` — which carry no commit and are not this mistake.

Stated as a limit, not a proof: the sweep reads one function at a time, so a
scope reaching a comparison under a name it does not seed would be invisible,
and it says nothing about ordering, hashing or `in`. It is a tripwire on the
shape that has actually been written five times.

## What the fix is worth, measured

Four paired runs of `doctor._build_or_refresh_generation`, three per arm per
scenario. The arms differ in exactly one thing: the "legacy" arm patches the two
comparisons back to whole-record equality. Sources are the live vault's real
files (883 of them) copied to a scratch checkout that holds still — a full build
on the live vault does not finish, it defers with `corpus_changed`, which is the
contract recorded on 2026-08-24.

**Idle pass — the corpus did not move, one commit happened.**

| | status | rebuilt | reused | wall (s) | CPU (s) |
|---|---|---|---|---|---|
| fixed | `current` | — | — | 4.04 / 4.07 / 10.38 | 4.01 / 4.06 / 4.26 |
| legacy | `built` | 883 | 0 | 138.24 / 142.93 / 152.23 | 135.17 / 135.83 / 136.92 |

This is `283eb3a`'s "643 s to 3.9 s", and the gate that decides it is
`doctor._parent_matches_identity`. One commit turned it off.

**Delta pass — one source of 883 changed, and a commit happened.**

| | rebuilt | reused | wall (s) | CPU (s) |
|---|---|---|---|---|
| fixed | 502 | 381 | 142.01 / 145.26 / 146.73 | 132.27 / 135.05 / 137.96 |
| legacy | 883 | 0 | 138.54 / 145.57 / 139.24 | 136.52 / 137.10 / 139.05 |

Record reuse comes back — 381 sources instead of none — **and buys no
measurable time on this corpus.** The difference sits inside the spread and its
sign differs between wall and CPU. Said plainly because the audit entry implied
otherwise: the expensive half of `283eb3a` is embedding reuse, and that is not
scope-gated, which is why the legacy arm still finishes in the same ~140 s while
reusing no records at all.

On the live vault, the same product function with the real active manifest
(`generation-18d015d5499fc78e-7d4cc1d8`, built at `3ad8f78`, live commit
`0ca2fc8`): `_parent_matches_identity` is `False` under the old comparison and
`True` under the new one, and `_scope_state` answers `superseded` both before
and after — unchanged.

## Sources

- [Value object — Wikipedia](https://en.wikipedia.org/wiki/Value_object)
- [Python Dataclasses: When to Avoid Them and Why](https://www.technetexperts.com/python-dataclass-red-flags/amp/)
- [Value Objects and Entities in DDD](https://hosseinnejati.medium.com/value-objects-and-entities-in-ddd-6e585a29b8fe)
- [Lint-as-architecture (standards = enforced rules), VirtusLab Visdom Maturity Matrix](https://visdom-maturity-matrix.virtuslab.com/guides/development/lint-as-architecture-standards-enforced-rules)
- [Custom lint rules from runtime bugs — Hacker News discussion](https://news.ycombinator.com/item?id=46675369)
- [ast-grep — Lint Rule](https://ast-grep.github.io/guide/project/lint-rule)
- [ESLint — Custom Rules](https://eslint.org/docs/latest/extend/custom-rules)

## Related

- `docs/research/2026-08-29-new-111-was-fixed-before-it-was-filed.md`
- `knowledge/notes/derived-evidence-generation-decision.md`
