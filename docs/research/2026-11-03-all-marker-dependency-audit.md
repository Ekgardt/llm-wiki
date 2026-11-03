# Audit every locked dependency variant

Date: 2026-11-03. Candidate CI correction, no installation or Python-floor change.

The requirements export contains environment markers. pip-audit's locked
RequirementSource evaluates those markers against its own interpreter and OS.
An all-extras/all-groups export therefore does not prove all-variant coverage.
A disposable resolution probe retained filelock 4.1.0 for older Python and 4.2.7
for newer Python. Auditing the requirements on Python 3.14 returned no known
vulnerabilities; auditing the same lock's pylock export found 17 advisories in
4.1.0 and none in 4.2.7. This is a reproduced coverage failure, independent of
the pending decision whether to raise the project's minimum Python version.

Use the existing hash-pinned pip-audit and uv export to add a universal pylock
pass. Export all extras/groups, excluding the project itself, into a dedicated
runner-temp directory; audit that directory with pip-audit --locked. Its pinned
PyLockSource enumerates every name/version without evaluating package markers,
including multiple versions of one name. Keep the existing requirements passes
and both advisory services. The OSV pass remains strict for collection failures,
including vendor local versions. No ignored advisories or replacement resolution.
A failed earlier pass may stop CI: green requires every pass to succeed.

Before implementation, prove export inventory equals uv.lock's registry package
name/version set, and exercise the actual pinned collector with two versions
under mutually exclusive markers. A workflow regression must require the universal
export and both locked-directory audit invocations. A small CI-only verifier
compares the successful OSV JSON report with every registry name/version in
uv.lock, rejecting missing or extra pairs, skipped records, known advisories,
empty coverage and unsupported source types. Only the local root project is
excluded. This gate also catches future collector changes that filter variants. Preserve existing assertions.
If a future collector filters variants, the fixture must fail. Unknown or missing
versions must remain visible failures through strict collection, not be dropped.

Alternatives: an OS/Python matrix repeats network work and can miss architecture
markers; removing markers from requirements encounters pip-audit's intentional
conflicting-version rejection; a bespoke advisory client duplicates existing
security tooling; adding OSV-Scanner would introduce another pinned binary when
the installed auditor already supports the standard lockfile format.

Primary sources checked today:
- Astral uv layout/export documentation: uv.lock captures cross-platform variants;
  pylock.toml is a supported export. https://docs.astral.sh/uv/concepts/projects/layout/
- PyPA pip-audit documentation and installed pinned source: --locked reads pylock,
  --strict fails collection errors. https://github.com/pypa/pip-audit
- OpenSSF OSV-Scanner documentation: resolved lockfiles identify package versions,
  with uv.lock/pylock support. https://google.github.io/osv-scanner/supported-languages-and-lockfiles/

Evidence: private logs/native-ci-audit-resolution-probe.json versus
logs/native-ci-pylock-audit.json; probe stays outside the candidate. This corrects
coverage only; it does not remediate the vulnerable dependencies or qualify native
Windows/macOS behavior. The Python-floor proposal still needs an explicit answer.
