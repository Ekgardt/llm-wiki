# What a keyword-adjacent match means

Date: 2026-08-26.
Question: `scripts/secret_redact.py` refuses 75 of 622 tracked members of this
repository, so the vault cannot be exported. Three of those refusals are a hex
id inside a URL path, the type annotation `lease_token: str`, and
`GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`. Before narrowing a fail-closed
security boundary, what do current scanners actually do about these, and is
narrowing the accepted practice or an invention of mine?

## Finding 1 — the keyword is proximity, never the finding

gitleaks' catch-all `generic-api-key` rule "searches for
`key|api|token|secret|client|passwd|password|auth|access` in close vicinity to
a random-looking string" — the keyword is a *locator*, and the verdict comes
from the value's entropy. Ours had no test on the value at all: the six
`NAME <sep> (\S+)` rules redacted whatever followed the name.

gitleaks has been pushed in exactly the direction this repository needs. Its
maintainers added exclusion patterns `secret.?name`, `client.?name` and
`key.?name` specifically "to filter out things that are **names** of secrets
rather than the secrets themselves" (PR #1587, issue #1578), alongside
stopwords for words that merely contain a keyword — `monkey`, `donkey`,
`keyboard`, `keystone`. That is the same family as this vault's own 2026-08-22
`sk-` boundary fix, and `${{ secrets.GITHUB_TOKEN }}` is its purest case: a
name of a secret, in a file whose whole point is that the value is elsewhere.

Yelp's `detect-secrets` reaches the same place from the other side. Its
`KeywordDetector` carries a `DENYLIST` of secret-sounding variable names
(`api_?key`, `private_?key`, `password`, `passwd`, `pwd`, `secret`, …) and then
constrains what may follow. Two details are directly usable:

- The value pattern ends `[^\v,\'"`]` — **a comma terminates the value.** So
  `SET lease_token=NULL,lease_expires_at=NULL` is not one value; it is `NULL`
  followed by another field.
- There are *quotes-required* regex variants per file type
  (`FOLLOWED_BY_EQUAL_SIGNS_QUOTES_REQUIRED_REGEX`; C++ requires quotes, YAML
  does not). The quoted/unquoted distinction is an established discriminator,
  not a guess: source code writes a literal in quotes and a reference bare.

## Finding 2 — a reference and a template are not secrets

`detect-secrets` ships a filter set that names our remaining two cases
outright (`detect_secrets/filters/heuristic.py`):

- `is_indirect_reference` rejects a value "resembling function calls or
  property access", giving `secret = get_secret_key()` and
  `secret = request.headers['apikey']` as its examples. That is
  `token = next(iterator)`, `token = lease.token`, `token = secrets.token_hex(32)`.
- `is_templated_secret` rejects `{secret}`, `<secret>` and `${secret}`, and
  `is_prefixed_with_dollar_sign` rejects a value beginning with `$`. Between
  them they are precisely `${{ secrets.GITHUB_TOKEN }}`.
- `is_not_alphanumeric_string` rejects a value with no letter at all — "clear
  false positives". Ours needed the mirror of it too: a value with *only*
  letters, which is a keyword or a type name (`str`, `None`, `return`).

So every narrowing this task requires is one a mainstream scanner already
makes. What is new here is only that they are applied at a fail-closed export
boundary rather than to a warning list.

## Finding 3 — entropy over identifiers is a known, unfixed failure

gitleaks issue #1830 reports that after 8.20.1 → 8.24.3 entropy detection began
"flagging plaintext identifiers", dictionary words and variable names, to the
point that "false positives outweigh true detections"; the thread shows no
maintainer fix, and the reporter's only mitigation is a growing regex
allowlist. The broader statement of the problem in the same corpus of issues is
worth recording verbatim in spirit: "looks like a secret" catches
high-entropy hashes, content digests, base64 test data, generated ids, and the
example credentials every README contains.

Two consequences for us:

1. A regex allowlist is the failure mode, not the fix — it is what the brief
   forbade, and the field's own experience says it becomes unwieldy at scale.
2. Raising the entropy threshold is described in the same material as "a
   genuine precision/recall trade-off: raise it too far and you start missing
   short, low-entropy-but-real secrets." That is why the threshold (4.0) and
   the run floor (40) are left untouched here and the fix is structural: a run
   containing `/` is judged by its separator-free pieces, because `/` is both a
   base64 character and a path separator. A blob keeps its randomness inside one
   run; `gist.github.com/karpathy/442a6bf…` spreads it across `com`, `karpathy`
   and a digest that is already exempt when it stands alone.

## What I did not adopt, and why

`detect-secrets` also ships `is_sequential_string`, which rejects values drawn
from ordered character sets — base64 alphabets, `abcdef…`, digit runs. That
single filter would clear a large share of this repository's *remaining*
refusals, because its own test fixtures are literally
`sk-abcdefghijklmnopqrstuvwxyz012345`, `ghp_abcdefghij…` and
`AAAABBBBCCCCDDDD`. It is principled and it is shipped by a serious tool.

I did not implement it in this change. It is a fourth cause, outside the three
the audit measured, and it changes which *real-looking* strings the boundary
stops — it deserves its own measurement rather than being smuggled in beside an
unrelated fix. Recorded here as the next candidate.

## Sources

- gitleaks, `generic-api-key` rule and its exclusion work — issue #1578
  ("Improving rule 'generic-api-key' to avoid false positives on e.g.
  `public_key=`, `monkey=`"), PR #1587 (`feat(generic-api-key): exclude
  keywords`), issue #1775 (Yocto/BitBake false positives).
  https://github.com/gitleaks/gitleaks/issues/1578 ·
  https://github.com/gitleaks/gitleaks/pull/1587
- gitleaks issue #1830, "Entropy detection is including all sorts of plaintext
  variable names and placeholders".
  https://github.com/gitleaks/gitleaks/issues/1830
- Yelp/detect-secrets, `detect_secrets/plugins/keyword.py` — `DENYLIST`,
  `AFFIX_REGEX`, the quotes-required regex variants, and the `SECRET` value
  pattern terminating on a comma.
  https://github.com/Yelp/detect-secrets/blob/master/detect_secrets/plugins/keyword.py
- Yelp/detect-secrets, `detect_secrets/filters/heuristic.py` —
  `is_indirect_reference`, `is_templated_secret`,
  `is_prefixed_with_dollar_sign`, `is_not_alphanumeric_string`,
  `is_sequential_string`.
  https://github.com/Yelp/detect-secrets/blob/master/detect_secrets/filters/heuristic.py
- This repository's own precedent:
  `docs/research/2026-08-22-secret-prefix-boundaries.md` (a prefix must start a
  token) and `docs/research/2026-08-25-which-secret-shapes-are-worth-a-pattern.md`.
