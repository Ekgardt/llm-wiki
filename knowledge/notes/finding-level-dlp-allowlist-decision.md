---
type: decision
status: accepted
confidence: high
source_authority: user
created: 2026-08-27
---

# Finding-Level DLP Allowlist

One-sentence summary: the DLP unlock names each refused finding by the hash of
its exact span, so an unrelated edit to the same file no longer re-blocks
export, while one new secret still does.

## Decision

`DLPPolicy` carries an optional `allow_finding_fingerprints` list: the SHA-256
of every span the scrubber replaced. A blocked payload passes only when it has
at least one finding and **every** finding's hash is in the list. Spans are
recovered by aligning the input with its scrubbed form; misalignment can only
change a hash and fail closed, never widen the unlock. A policy without the
key keeps its pre-extension digest, so existing authenticated policies stay
valid. Regeneration is one deliberate operator command:
`export_vault.py --write-allow-policy <path>`.

## Why

The first application of the 2026-08-25 exact-content allowlist fingerprinted
33 whole files; any edit to any of them re-blocked export until the policy was
rebuilt by hand — a maintainability defect the owner named on 2026-08-27.
Practice agrees on the unit of exception: detect-secrets baselines store the
hash of the secret value itself, gitleaks ignores findings by fingerprint so
suppressions survive rebases. Neither allowlists files.

## Rejected

- Inline allow-markers: the refused files are test fixtures whose exact bytes
  are compared by tests; a marker would change the data under test.
- Path-scoped allow (`tests/`): a real secret dropped into a test would pass
  silently — narrowness by value is stricter than narrowness by path.
- Whole-file fingerprints: the defect this decision exists to remove.

## Source

- `docs/research/2026-08-27-allowlist-the-finding-not-the-file.md`
- `scripts/model_dlp.py` (`allow_finding_fingerprints`, `_finding_spans`),
  `scripts/export_vault.py` (`--write-allow-policy`),
  `tests/test_dlp_finding_allowlist.py`

## Related

- [[secret-shape-not-secret-name-decision]] — what counts as a finding at all.
