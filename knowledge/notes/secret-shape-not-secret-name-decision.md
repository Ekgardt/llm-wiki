---
type: decision
title: "A Credential Name Is Not A Credential"
description: "The redactor decides on the value, not the key that precedes it; a slash run is judged by its pieces; and --strict compares the archive against the working tree instead of asking git status."
date: 2026-08-25
confidence: high
source_authority: user
status: active
---
# A Credential Name Is Not A Credential

One-sentence summary: A key named `token` proves nothing about what follows it,
a hex digest inside a URL is the same digest it is on its own, and `--strict`
means "the archive matches the working tree" rather than "git status is empty".

## Decision

Date: 2026-08-25.

### 1. The value decides, not the key

`scripts/secret_redact.py` had six rules of the form `NAME <sep> (\S+)` that
redacted whatever followed a credential-shaped name. The name only marks a slot.
A slot can be declared, computed, or pointed at without ever holding a secret:

| what was written | what the rule read |
|---|---|
| `lease_token: str` | a type annotation |
| `token = next(iterator)` | an expression |
| `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` | a reference to a secret |
| `SET lease_token=NULL,lease_expires_at=NULL` | a SQL statement |
| `class CancellationToken:` | a block opener |

A match is now a finding only when the value is a **credential literal**:

- quotes are stripped, and a trailing `,` or `;` is dropped;
- it carries no call, subscript, generic, placeholder, escape, interpolation or
  comma — `()[]{}<>$\?*|&;,`;
- it is not an integer;
- it is at least 8 characters — below that a value cannot be told from a
  keyword, a type name or a small number, and the false refusal costs more than
  the missed short secret;
- if it is **unquoted**, it is neither an all-letter word nor a symbol
  reference (`owner_token`, `lease.token`, `NO_CONTRADICTIONS`). Source writes a
  reference bare and a literal in quotes, so `"my_secret_value"` is still caught.

The separator may not cross a line. `.env`, YAML, JSON, HTTP headers and source
assignments all put the value beside the key; a name and a colon at end of line
opens a block. A YAML scalar indented onto the next line is the price, named
here rather than silently paid.

### 2. A slash run is judged by its pieces

`/` is both a base64 character and a path separator, so the entropy rule could
not tell `gist.github.com/karpathy/442a6bf555914893e9891c11519de94f` from a
payload: the digest alone is exempt (`_PURE_HEX_RE`), but joining it to `com`
and `karpathy` pushed the run over the entropy threshold.

A run containing `/` is now a path unless one of its separator-free pieces is a
blob on its own — at least 16 characters, not all letters, not pure hex, and
over the entropy threshold. A blob keeps its randomness inside one run; a path
spreads it over several meaningful ones. The all-letters clause was measured,
not guessed: `CreatingLaunchdJobs` in an Apple documentation URL scores 4.04,
just over the threshold, and base64 of random bytes does not go sixteen
characters without a digit.

The pre-existing shape rule (a one- or two-character piece means a path) is kept
ahead of this one, so the macOS temporary-directory case of 2026-08-18 is
unchanged.

Nothing here touches the entropy threshold (4.0) or the run floor (40), and no
rule is exempted by path. The still-catching cases are held by
`tests/test_secret_redact.py`: an npm-integrity base64 blob containing three
slashes, and a random token inside a URL path, both still redacted.

### 3. `--strict` compares the archive against the working tree

`scripts/export_vault.py --strict` asked `git status --porcelain`, which answers
a wider question than the flag means. It reports untracked files, which
`git archive` can never carry; and since the vault and the checkout became one
directory (2026-08-21) it reports the tracked `knowledge/index.md` and
`knowledge/log.md` that the runtime rewrites on every compile. So `--strict`
refused every export in the installed vault and named nothing.

`--strict` now fails when a **tracked path the archive carries** differs from the
working tree, and names those paths (`git diff --name-only -z <ref>`; `-z`
because this repository has already been bitten by the porcelain status column
and by C-quoted paths). Untracked files no longer refuse an export they cannot
enter. This follows the line already drawn for the nightly self-update in
[[knowledge/notes/automatic-code-update-decision]]: the question is never "is the
tree clean" but "does this operation touch something that changed".

What this deliberately does **not** do is make `--strict` pass while a compile
has left `knowledge/index.md` and `knowledge/log.md` rewritten. There the
statement "the archive does not match your working copy" is simply true, and a
flag that asserted otherwise would be lying to the only person it exists to
warn. The change is that the refusal now names those two files, so the operator
can see it is the runtime's own bookkeeping and either commit or drop the flag,
instead of meeting an opaque refusal that also fired on untracked scratch.

Note for anyone reproducing this: `export_vault` runs git in `memory_state.ROOT`,
the canonical checkout, not in the caller's worktree.

## What this does not claim

The boundary stays fail-closed and no path is exempt. Two limits are real and
named rather than hidden:

- An unquoted bare snake_case value with no digits is read as a symbol
  reference. A vendor prefix outranks that rule — `ghp_…`, `github_pat_…`,
  `npm_…` and `hf_…` are all identifier-shaped and are all still caught, and a
  regression test holds it — so what remains uncovered is an unprefixed secret
  written bare as `token = my_secret_value`. Quoting it brings it back.
- A YAML scalar indented onto the line below its key is no longer read.

Rule order is load-bearing and was nearly lost: the key/value rules were the
first six entries of one pattern list, so `token=sk-…` collapsed to
`token=[REDACTED]`, not `token=[REDACTED_API_KEY]`. Splitting them into their
own pass silently renumbered that and eleven tests in `test_plugin_helpers.py`
and `test_memory_queue.py` failed on the marker — which is asserted, hashed and
stored downstream. The named-value pass runs first, as before.

## Measured result

`git archive` of `HEAD`, every member through `_scan_tar_member`: **75 of 622
members refused before, 30 after.** Everything the audit named — `README.md` and
both translations, `.github/workflows/tests.yml`, `scripts/blackboard.py`,
`scripts/claims.py`, `benchmark/model-matrix-v1.json`, the eight
`docs/superpowers/` pages and both published decision pages — now passes.

The 30 that remain are not false positives. Twenty-six are `tests/` files that
hold credential-shaped fixtures on purpose, because they are the tests for this
machinery (`sk-abcdefghijklmnopqrstuvwxyz012345`, `AKIAIOSFODNN7EXAMPLE`, a JWT,
a PEM block, `token="first-token"`). Four hold genuine base64: the pinned
Pyright `sha512-` integrity hash in `scripts/pyright_profile.py`,
`tests/test_pyright_profile.py` and its plan page, and a base64
prompt-injection payload in `benchmark/adversarial-retrieval-v1.json`.

So the export of this repository is still refused, and refused for the right
reason. Clearing it means content the operator vouches for, not a path rule:
`DLPPolicy.allow_fingerprints` already allowlists an exact payload by SHA-256,
so one edited byte kills the exemption. Whether to carry such a policy is the
owner's call and is not made here.

## Source / Evidence

- `scripts/secret_redact.py` — `_value_is_credential`, `_segment_is_blob`,
  `_looks_like_path`.
- `scripts/export_vault.py` — `_paths_differing_from_ref`,
  `_report_strict_drift`.
- `tests/test_secret_redact.py`, `tests/test_export_strict.py`.
- Counts measured with `git archive HEAD` piped through `_scan_tar_member`.

## Related

- [[knowledge/notes/automatic-code-update-decision]] — the same rejection of
  "the tree must be clean" in favour of "this operation must not touch a
  changed path".
- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]] —
  the one fail-closed DLP boundary this narrows without opening.
- `docs/research/2026-08-22-secret-prefix-boundaries.md` — the earlier fix in the
  same family: a prefix must start a token.
