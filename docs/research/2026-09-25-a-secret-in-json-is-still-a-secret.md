# A secret written as JSON is still a secret

Date: 2026-09-25. Audit item A-2 of `docs/AUDIT-2026-09-25-full.md`.

## Question

`secret_redact.redact_secrets` is the one scrub before a provider call, the daily
log and the session record. What forms does it miss, and what should it catch?

## Sources

- gitleaks default configuration, rule `generic-api-key` (fetched 2026-09-25,
  https://raw.githubusercontent.com/gitleaks/gitleaks/master/config/gitleaks.toml):
  the key is any name that contains `access|auth|api|credential|creds|key|
  passw(or)?d|secret|token` with up to 20 more word characters, quotes and spaces
  may sit between the name and the separator (`[\s'"]{0,3}`), and the value may be
  quoted. Rule `gitlab-pat`: `glpat-[\w-]{20}`. Rule `curl-auth-header`:
  `Authorization: Bearer <token>` inside a quoted header.
- RFC 3986, section 3.2.1 (fetched 2026-09-25,
  https://www.rfc-editor.org/rfc/rfc3986#section-3.2.1): `user:password@host`
  userinfo is deprecated precisely because it puts a password in the URI.

## Findings (facts, run 2026-09-25 against the module)

Passed through unchanged: `{"api_key": "a8f3k29dkq84mzp1x"}`,
`{"password": "Hunter2024!xyz"}`, `"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"`,
`postgres://admin:S3cr3tPass@db.example:5432/app`,
`AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`,
`glpat-xxxxxxxxxxxxxxxxxxxx`. Caught: `api_key=a8f3k29dkq84mzp1x`.

Causes, in `_NAMED_VALUE_PATTERNS` and `_value_is_credential`:
1. The separator must follow the name directly, so a JSON key's closing quote
   (`"api_key": `) breaks the match.
2. The name must be the whole word (`secret`, `token`), so `AWS_SECRET_ACCESS_KEY`
   and `client_secret` do not match.
3. The value runs to the next whitespace, so a JSON value takes its `"}` with it,
   and `}` marks it as code.
4. `$&*?|` mark any value as code, quoted or not, so a quoted password containing
   one is kept.
5. No rule covers URL userinfo or GitLab tokens.

## Decision (conclusion)

- The key is any name containing a credential word (`passw(or)d`, `pwd`,
  `secret`, `token`, `api key`, `access key`, `private key`, `credential(s)`),
  with an optional quote before the separator, as in gitleaks.
- The value ends at `,`, `;`, `}` or `]`; its quotes and what follows it are kept,
  so a JSON line stays JSON: `"api_key": "[REDACTED]"}`.
- Inside quotes only interpolation (`${`, `$(`, `{{`) makes a value code; a bare
  value keeps the wider code test, which is what stops `token = next(iterator)`.
- New rules: URL userinfo passwords (`scheme://user:[REDACTED]@host`) and GitLab
  personal tokens (`[REDACTED_GITLAB_TOKEN]`).

## Edited files

- `scripts/secret_redact.py`
- `tests/test_a_secret_in_json_is_still_a_secret.py` (new) and the redaction tests
  that pinned the replaced quoted form

## Uncertainty

A broader key raises the chance of redacting a harmless value beside a name like
`token_budget`; values must still be eight characters, non-numeric and not a bare
identifier, which the existing tests of code forms hold.
