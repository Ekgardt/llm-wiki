---
type: decision
status: accepted
confidence: high
source_authority: user
created: 2026-08-28
---

# A Provider Call Runs Outside The Vault

One-sentence summary: every provider subprocess starts in an empty directory
outside the vault, because inheriting the vault as its working directory made
a CLI that discovers project memory load this repository's `CLAUDE.md` — with
the index and log it imports — before it ever saw the prompt, and that fixed
overhead was the whole distance between an answer and a timeout.

## Decision

`llm_client.provider_cwd()` yields an empty temporary directory outside the
vault for the duration of one call, and every provider subprocess — the Claude
generation call, the Codex generation call, and the Claude capability probe —
runs in it. The directory is created per call and removed after. OpenCode is
unaffected: it is an HTTP backend with no child process.

The directory must be outside the vault tree. Project-memory discovery walks
upwards, so an empty directory under `cache/` would find the same `CLAUDE.md`
one level up and change nothing.

The user's global memory (`~/.claude/CLAUDE.md`) still loads. It does not
depend on the working directory, it is small, and removing it is neither
required nor claimed.

## Why

Measured 2026-08-28, paired, same machine, trivial prompt:

| working directory | try 1 | try 2 |
|---|---|---|
| the vault | 62.15 s | 64.60 s |
| `/tmp` | 27.24 s | 33.90 s |

About 33 seconds of fixed overhead, independent of prompt size, against a 90
second ceiling (`MEMORY_LLM_TIMEOUT_S`). The live vault was failing its compile
with exactly that: `last_compile_error: no LLM provider produced a validated
compile plan: … draft:claude:<implicit>:provider_timeout` at 16:03:47.

A memory call is an internal service call, not an agent's turn in the
operator's project. The material is already in the prompt and the answer's
shape is fixed by a schema, so the working directory buys nothing and cost
double.

The `--setting-sources` flag added 2026-08-26 against the operator's persona
does not cover this: it suppresses settings *files*, while project memory is
discovered from the working directory.

## Evidence

After the change, measured through the product's own call path: 26.18 s and
13.29 s — what a neutral call costs, not double it.

## Source

- `docs/research/2026-08-28-where-the-provider-runs.md`
- `scripts/llm_client.py` (`provider_cwd`)
- `tests/test_provider_runs_outside_the_vault.py`

## Related

- [[knowledge/notes/oversized-daily-compile-decision]] — the other half of the
  same ceiling: what to do when the work itself, rather than the overhead
  around it, does not fit the budget.
