# Which secret shapes are worth a pattern (2026-08-25)

## The question

The redactor knows `sk-`, `ghp_`, `xox[baprs]-`, `AKIA`, `AIza`, JWTs, PEM
blocks, `key=value` forms, and a Shannon-entropy fallback. Register items
`OPEN-002/003/009` call the detector "regular expressions and entropy, not a
full DLP". Which shapes are missing that a 2026 scanner would catch?

## What the practice says

GitHub's secret scanning shipped its largest single batch in March 2026 —
twenty-eight detectors across fifteen providers — and added more in June
([March update][march]; [June update][june]; [coverage update][coverage]).
Two lessons carry over to a small local redactor:

1. **Prefixes are the signal.** Providers deliberately give tokens a fixed,
   greppable prefix so scanners can catch them cheaply; the practice write-ups
   call these "prefixed secrets" and recommend requiring a distinguishing prefix
   even for internal credentials ([Semgrep on prefixed secrets][semgrep]).
2. **A prefix does not identify the vendor.** `sk_live_` is Stripe *and*
   APIDeck; `sk_` is DeepSeek and legacy OpenAI. A redactor must therefore
   redact on shape and must not claim in the replacement text which service the
   secret belonged to.

The shapes missing here, by that reading: GitHub's other five prefixes
(`gho_`, `ghu_`, `ghs_`, `ghr_`, and fine-grained `github_pat_`), Stripe-style
`sk_live_`/`sk_test_`/`rk_live_`/`rk_test_` (underscore, so the existing `sk-`
rule never saw them), Slack app tokens `xapp-`, npm `npm_`, Hugging Face `hf_`,
PyPI `pypi-`, and Google OAuth client secrets `GOCSPX-`.

## What this vault does

Adds those shapes to the same table, with the same token-boundary guard that
2026-08-22 added after `sk-` matched inside an ordinary page slug, and with
generic replacement names: `[REDACTED_API_KEY]` rather than a vendor claim the
prefix cannot support. Entropy stays as the fallback for shapes nobody
published.

What is still true and still recorded: this is a redactor, not a DLP product.
It removes secrets, not private prose, and no pattern list is ever complete —
which is exactly why the entropy fallback and the fail-closed boundary exist.

[march]: https://github.blog/changelog/2026-03-10-secret-scanning-pattern-updates-march-2026/
[june]: https://github.blog/changelog/2026-06-17-secret-scanning-updates-june-2026/
[coverage]: https://github.blog/changelog/2026-03-31-github-secret-scanning-nine-new-types-and-more/
[semgrep]: https://semgrep.dev/blog/2025/secrets-story-and-prefixed-secrets/
