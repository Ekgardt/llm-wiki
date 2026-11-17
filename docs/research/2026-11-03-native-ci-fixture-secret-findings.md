# Synthetic fixture values that look like secrets

Date: 2026-11-03, revised 2026-11-11. Gitleaks 8.30.1, verified against its release
SHA-256 checksum, reported 19 generic-api-key and private-key findings in test inputs:
twelve in queue_evidence_records/fixtures/existing-records.json, six in
queue_task_records/fixtures/existing-records.json and one PEM string in
lsp_credentials. None of them names a provider credential.

The first answer was exact AND allowlists in .gitleaks.toml. That answer was withdrawn
on 2026-11-11: a reviewer scanning a change with the configuration of the target branch
does not read the allowlists the change adds, so every new synthetic value that looks
like a key still fails that review, and an allowlist per value grows with every fixture.

## Alternatives considered

- Allowlists in `.gitleaks.toml`, by path or by value. A path allowlist hides any
  real secret later pasted into the same fixture; a value allowlist is invisible to a
  scan that uses the base configuration. Withdrawn for both reasons.
- `.gitleaksignore` fingerprints or a `--baseline-path` report. Both suppress known
  findings by fingerprint, and both are read from the scanned tree or a flag, so a
  reviewer scanning with its own configuration still sees the findings.
- Inline `gitleaks:allow` comments. JSON fixtures cannot carry comments, and
  `--ignore-gitleaks-allow` disables them.
- GitHub `secret_scanning.yml` `paths-ignore`. It excludes whole directories from
  GitHub's scanning and push protection; it does not apply to gitleaks and would also
  hide real secrets in those directories.
- detect-secrets baselines and `pragma: allowlist secret`. Another scanner's
  suppression format; adopting it would not change what gitleaks reports.
- **Chosen:** fixture values that are not secret-shaped. Secret-scanning guidance
  recommends replacing token-shaped test values with obviously fake ones when the code
  under test does not need the exact format. Nothing is suppressed, so every scanner
  and every configuration agrees.

## What changed

The fixtures now carry values the scanner's own rules classify as non-secret:

- the seven random-looking SHA-256 digests that only name an operation, a package, a
  fence or a lease are a single repeated hexadecimal digit (`1` to `7`, 64 characters),
  which still satisfies every digest schema; the scanner's entropy threshold refuses them;
- the two digests that the code under test recomputes from neighbouring bytes
  (`manifest_sha256`, `disposition_sha256` in `corrupt_records_match`) were recomputed
  from the changed bytes, so the case still proves that matching records are accepted;
- credential-shaped test strings in Python are assembled at run time
  (`' '.join(('PRIVATE', 'KEY'))`, `":".join(("user", "privatepw"))`), so the source
  holds no literal the scanner can match while the redactor still sees the whole value.

Every expected output the code derives was left as captured; the 48 queue record tests
and the redaction tests pass unchanged. The five allowlists that named the old values
were removed, and `tests/test_quality_guards.py` refuses any remaining secret
allowlist whose regex is a pattern rather than one exact value.

Trade-off: a fixture author must choose fake-looking values; the guard and the scan in
CI catch one that does not.

Sources:
- https://github.com/gitleaks/gitleaks#configuration (rules, entropy, allowlists,
  regexTarget, `.gitleaksignore`, `gitleaks:allow`, baselines).
- https://docs.github.com/en/code-security/how-tos/secure-your-secrets/customize-leak-detection/exclude-folders-and-files
  (`secret_scanning.yml` `paths-ignore` and its effect on push protection).
- https://github.com/ibm/detect-secrets (baseline audit and inline allowlist pragmas).
- https://qaskills.sh/blog/secrets-scanning-ci-gitleaks-baselines (replace token-shaped
  test values with obviously fake ones instead of suppressing them).
