# A local build is not an audit skip

Research date: 2026-09-29. The current lock export contains 115 dependencies.
The default PyPI run of pip-audit 2.10.1 reports no known vulnerabilities but
skips `torch 2.14.0+cpu`: that exact local version is not hosted on PyPI. Thus
the first report covers 114 packages, not all 115. Adding `--strict` makes the
same single-package PyPI check fail, confirming that the missing check matters.

The installed CPU build reports version `2.14.0+cpu` and source commit
`08187d9e0fba026dc8217405802ab5381dc88d90` in its version file. The lock retains
its vendor index and artifact hashes. This work does not replace the artifact,
strip its local suffix, or claim that all downstream builds equal an upstream
wheel. The upstream 2.14.0 release also exists on PyPI and the vendor's release
page; that alone does not audit this CPU artifact.

The same pinned pip-audit supports OSV. A strict OSV run on the exact lock export
returns 115 dependency results, no skips and no known vulnerability matches.
A separate metadata-only positive control using `torch==2.5.1+cpu` returns a
nonzero status and identifies known vulnerabilities, including `PYSEC-2025-41`
for the vendor's `weights_only=True` loading vulnerability. No vulnerable
package was installed or executed for this control.

Keep the existing PyPI audit and add a strict OSV audit of the same hashed lock
export in the existing CI job. Replacing PyPI would remove an existing advisory
check; suppressing the CPU dependency or accepting a skipped result would leave
the blind spot. Installing a different, CUDA-bearing artifact to suit the
scanner would change the product unnecessarily. The additional feed adds
network requests and another service whose failure can block CI, justified by
the demonstrated gap. It adds no package, model call, product runtime service,
path, environment contract or persisted data format.

Primary sources checked on 2026-09-29:

- [PyPA pip-audit](https://github.com/pypa/pip-audit): selectable advisory
  services, strict dependency collection and auditing locked requirements.
- [OSV query API](https://google.github.io/osv.dev/post-v1-query/): package
  ecosystem and exact version form an advisory query. An empty result means
  no matching known advisory, not proof that a package is secure.
- [PyTorch's vulnerability disclosure](https://github.com/pytorch/pytorch/security/advisories/GHSA-53q9-r3pm-6pq6):
  affected releases through 2.5.1 and the 2.6.0 fix establish the positive control.
- [Python packaging local versions](https://packaging.python.org/en/latest/specifications/version-specifiers/#local-version-identifiers):
  local labels distinguish builds and are not accepted on PyPI; deleting one
  is not a general proof of source or security equivalence.

The CI policy regression must fail when the exact-version OSV pass or strict
failure behavior is missing, while retaining the hashed export, pinned audit
tool and existing PyPI pass. The live positive control and actual lock audit
are separate evidence from that policy test. Other platforms' selected package
sets and unknown vulnerabilities remain outside this local result.
