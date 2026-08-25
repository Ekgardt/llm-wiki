# Prefix rules in secret detection need a token boundary (2026-08-22)

## Why this was researched

`scripts/secret_redact.py` refused every knowledge write in this vault. The
fail-closed DLP boundary in `scripts/markdown_transaction.py` calls
`redact_secrets` on the exact after-image bytes and quarantines the transaction
when the text changes. Two compile transactions were quarantined with
`dlp_content_blocked` (2026-08-22T07:28:49Z and 2026-08-22T10:45:41Z), and the
match was inside a page slug:

    dead-task-retirement-and-restore-decision
            ^^^--- `sk-` plus 30 more `[A-Za-z0-9_-]`

The rule `sk-[A-Za-z0-9][A-Za-z0-9_-]{18,}` cannot tell that substring from an
OpenAI key. The file is in the top decile of this project's afferent coupling,
so rule 2 applies before changing it.

## What current practice says

The failure mode is named and its fix is standard. Semgrep's write-up on
prefixed secrets makes the distinction this vault needed: tightly prefixed
provider keys (`sk_live_…`) are precise, while rules that fall back to keyword
or substring matching are where precision breaks, and a lazily written
case-insensitive substring "can also match inside words like `fraudDetection`,
`audit_log`, and `inaudible` — each a false positive — but the fix is tiny, just
a word boundary in the expression."

Gitleaks documentation and guides make the same point with an example that is
this defect exactly: with `\bsk_12345\b` the scanner matches only when the token
is surrounded by non-word characters, and in `ask_12345` the leading `a` is a
word character, so the boundary rejects it. The other lever those guides offer —
an allowlist entry per false positive — is the wrong tool here: the collision is
structural (any slug containing `task-` and enough hyphenated words), so it
would have to be re-added for every future page.

Entropy scoring, which gitleaks also uses, does not help: the substring is
low-entropy English and is caught by the literal prefix rule, not by the
entropy rule.

## What this changes here

Each literal-prefix rule gets a `(?<![A-Za-z0-9])` guard: `sk-`, `ghp_`,
`xox[baprs]-`, `AKIA`, `AIza`, and the JWT `eyJ…` triple. Punctuation is
deliberately still a boundary, so `OPENAI_API_KEY=sk-…`, `"sk-…"` and `(sk-…)`
are caught exactly as before; only a prefix continuing an alphanumeric word is
rejected. `_` and `-` are left out of the guard on purpose: a real key pasted
after them is more plausible than a false positive, and this boundary is the
conservative direction for a fail-closed check.

The keyword rules (`api_key=`, `token:`, `password=`, …) are untouched — they
anchor on their own keyword and did not produce this failure.

## Sources

- Semgrep, "Secrets Story: The Prefixed Secrets That Tried to Get Away" —
  https://semgrep.dev/blog/2025/secrets-story-and-prefixed-secrets/
- Steve Kinney, "Secret Scanning with Gitleaks" (word-boundary example
  `ask_12345` vs `sk_12345`) —
  https://stevekinney.com/courses/self-testing-ai-agents/secret-scanning-with-gitleaks
- Cremit, "Secret Scanning False Positives: Why They Happen and How to Eliminate
  Them" — https://www.cremit.io/blog/secret-scanning-false-positives-causes-and-fixes
- "Secrets Scanning in Git and CI with Gitleaks | 2026 Guide" —
  https://khimananda.com/blog/secrets-scanning-in-git-and-ci-with-gitleaks

## Open question

Whether the entropy rule has the same shape of false positive on this vault's
own content is not answered here. It did not fire on the blocked pages, and
nothing was measured for it.
