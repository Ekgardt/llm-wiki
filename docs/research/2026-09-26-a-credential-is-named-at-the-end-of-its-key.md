# A credential is named at the end of its key

Date: 2026-09-26. Audit 2026-09-26 items A-2, B-2 and B-3 (docs/AUDIT-2026-09-26-full.md).

## Fact
- B-3 (my regression of the 2026-09-25 A-2 fix), reproduced by the audit:
  `api_key = os.environ["API_KEY"]` → `api_key = [REDACTED]"]`;
  `tokenizer_name: intfloat/…`, `TOKENIZER_PATH=/opt/…`, `PWD=/srv/app`,
  `NEXT_PUBLIC_TOKEN_URL=https://…`, `password_reset_url: /accounts/reset/`,
  `"secretName": "prod-db-credentials"` were redacted; `http://localhost:8080@`
  became `http://localhost:[REDACTED]@`. Causes in `secret_redact.py`: the
  credential word may sit anywhere inside a key (`[\w.-]{0,20}` after it); a
  lone trailing quote counts as "quoted" (`_unquote` strips either end); the URL
  userinfo rule takes a port for a password.
- B-2: still passed through: `curl -u admin:S3cret…`, `mysql -pS3cret…`,
  `--password …`, `Authorization: Basic …`, a quoted value with spaces
  (`password="correct horse battery staple"`), long all-letter values
  (`client_secret=abcdefghijklmnop`); `postgres://user:p@ss@host` left `@ss`.
- A-2: in raw JSONL the value regex `\S+` swallowed the `\"` that ends a JSON
  string, the line stopped being JSON and the session record dropped the turn
  (96 of 57 412 live lines). Escaped keys (`\"password\": \"…\"`) were not
  matched at all.

## Sources (fetched 2026-09-26)
- curl manual, https://curl.se/docs/manpage.html: "-u, --user <user:password>
  Specify the username and password to use for server authentication."
- MySQL 8.4 Reference Manual, connection options,
  https://dev.mysql.com/doc/refman/8.4/en/connection-options.html: for
  `--password[=pass_val], -p[pass_val]`, "If given, there must be no space between
  --password= or -p and the password following it."

## Decision
- The credential word must end the key (a digit or separator suffix is allowed:
  `DB_PASSWORD`, `token2`, `AWS_SECRET_ACCESS_KEY`); `tokenizer_name`,
  `token_url`, `secretName`, `password_reset_url` are not credential keys. `PWD`
  and `OLDPWD` are shell directory variables and are excluded by name.
- A value is quoted only when the same quote opens and closes it; a quoted value
  is taken whole (spaces included) to its closing quote on the same line.
- A value stops before a backslash, so a JSON string keeps its escaped closing
  quote; keys and values written with escaped quotes (`\"password\": \"…\"`) are
  matched.
- A value that is a path or a URL (`/…`, `~/…`, `X:\…`, `scheme://…`) is not a
  credential.
- New rules: `-u/--user user:password` (the password part), `mysql … -pVALUE`,
  `--password=VALUE` / `--password VALUE`, `Authorization: Basic|Token …`.
- An unquoted all-letter value of 12 characters or more after a credential key is
  redacted; shorter words (`required`, `optional`) stay.
- The URL userinfo rule takes the password up to the last `@` of the authority and
  never a port-only value.

## Files
- scripts/secret_redact.py
- tests/test_a_credential_is_named_at_the_end_of_its_key.py
