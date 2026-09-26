# The redactor's test secrets are declared to gitleaks

Date: 2026-09-26.

## Facts

- `gitleaks git --log-opts=295ece8f..039ffd87` reports three findings, all in
  `tests/test_a_credential_is_named_at_the_end_of_its_key.py` of commit 8d723839:
  the invented value `S3cretPassw0rd` and the lines built from it. They are the
  inputs the redactor must hide; nothing real.
- gitleaks README (https://github.com/gitleaks/gitleaks, fetched 2026-09-26):
  "Each leak, or finding, has a Fingerprint that uniquely identifies a secret. Add
  this fingerprint to the `.gitleaksignore` file to ignore that specific secret",
  and "If you are knowingly committing a test secret that gitleaks will catch you
  can add a `gitleaks:allow` comment to that line".

## Decision

- The three fingerprints of commit 8d723839 go into `.gitleaksignore`, so the
  history scan of the pull request passes; the three lines carry
  `# gitleaks:allow`, so a scan of the working tree passes too. The repository's
  own `tests/test_security_invariants.py` already uses the inline form.

## Files

- `.gitleaksignore`
- `tests/test_a_credential_is_named_at_the_end_of_its_key.py`
