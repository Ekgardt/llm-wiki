# CI failures keep their checks

Date: 2026-09-30. Status: repairs under qualification.

PR #53's initial CI failed the existing address-privacy guard on a literal
Python source string containing backslash-n immediately before @router.get.
The regex joins the escaped newline marker to the decorator and sees an address. It is generated public test source,
not a real private address. Write the fixture as actual multiline Python source;
retain the privacy guard and the identical long-route extraction assertions.

The same CI reports 16 current dependency advisories in PyJWT 2.13.0 and
virtualenv 21.2.4. Update the exact lock, including hashes, through uv's resolver;
keep both vulnerability feeds and strict collection failure. No ignored advisory,
new runtime dependency or change to supported Python versions is introduced.

Primary sources checked today:

- [PyJWT changelog](https://pyjwt.readthedocs.io/en/stable/changelog.html)
  and its [releases](https://github.com/jpadilla/pyjwt/releases) describe the
  2.15 security corrections and the 2.15.1 compatibility fixes.
- [virtualenv changelog](https://virtualenv.pypa.io/en/latest/changelog.html)
  describes shell/Windows activation injection and seed-wheel verification
  corrections through 21.7.13.
- [uv dependency management](https://docs.astral.sh/uv/concepts/projects/dependencies/)
  describes selected upgrades through the existing dependency constraints.

Choose PyJWT 2.15.1 and virtualenv 21.7.13. virtualenv requires python-discovery
>=1.6, so that transitive upgrade is necessary. An attempted retention of 1.2.2
was correctly refused by the resolver; supported platforms were not weakened.
The first unconstrained resolution selected virtualenv 21.14.1; the selected
lock uses the smaller security update to reduce unrelated changes. Leaving the
old versions or suppressing the advisories fails security qualification.
Updating the lock is distinct from installing packages in the running vault;
only claim installation after its separate verification.

Evidence: logs/audit-2026-09-30-finish-ci-dependency-raw.txt;
logs/audit-2026-09-30-finish-ci-py314-raw.txt;
logs/audit-2026-09-30-finish-security-lock-selected.txt;
logs/audit-2026-09-30-finish-security-lock-final.txt.


Both exact-lock vulnerability feeds now pass: PyPI names no known advisories
and explicitly cannot resolve the CPU-local torch version; the additional strict
OSV feed covers that exact local version and passes. Hash-verified isolated
packages pass 70 HTTP/security/dependency checks. The exact staged public tree
passes 40 privacy/dependency checks (1 skipped). That check also caught this
research prose repeating the same synthetic address spelling; the prose was
corrected and the unchanged guard rerun. Gitleaks scanned the public export
without matches. A full regression run remains in progress against the prior
installed package versions; it cannot alone qualify the dependency installation.

Evidence: logs/audit-2026-09-30-finish-security-pypi.txt;
logs/audit-2026-09-30-finish-security-osv.txt;
logs/audit-2026-09-30-finish-security-overlay-tests.txt;
logs/audit-2026-09-30-finish-exact-public-final-tests.txt.
