# Python 3.14 qualification follow-up

Date: 2026-11-03. Native evidence: workflow41633236235 at e2d9d318.
No failed gate is waived and no benchmark measurements are rewritten.

1. Code-navigation qualification still explicitly requires machine-readable
Python3.10.x even though the approved runtime is3.14. Linux native correctness
passed, all measured performance/correctness values were within existing limits,
but the obsolete series gate marked the evidence incomplete. Update the series
predicate and current fixture manifest/schema to3.14; retain Linux-only measured
qualification and every identity, correctness, timing/resource gate. Old3.10
reports remain historical, not current qualification. Test3.14 acceptance and
other/malformed versions refusal. Re-evaluate retained real native report without
changing any reported measurement, and retain the need for new native validation.

2. Adding failure artifact retention exposed a CI test which treated a `uses`
step as an empty shell command. Collect only actual `run` steps, retaining its
assertion that nightly is the last executed command and follows both installs.
The artifact upload itself rejected `..` in the Windows path. Canonicalize the
existing runtime path before installation using the selected interpreter; this
changes no runtime location. Preserve failure diagnostics without suppressing the
original compile failure. Its underlying processor_exception remains unresolved
until diagnostic bytes are available.

3. Updating all model-related packages invalidated the benchmark's frozen package
binding. The validator correctly rejected it. Do not alter report versions,
package-map hashes or quality values, and do not relax current-lock membership.
A disposable compatibility probe keeps the measured numpy2.5.1,
sentence-transformers5.6.0, torch2.13.0 and transformers5.13.0 while retaining the
security updates (including filelock4.2.7) and Python3.14. The resolver also selects
tokenizers0.22.2 required by that transformers version; jieba remains0.42.1.
All122 records resolve. Linuxx64/Windowsx64/macOS15ARM64 full+dev+audit no-build
dry runs pass; generic macOS target without deployment15 has no ONNX wheel, as
already documented. Both PyPI and strictOSV audits report no known advisories;
exact universal name/version coverage is verified separately.

Prefer these compatible measured versions over an unmeasured model-stack upgrade
in a behavior-preserving decomposition. Make constraints explicit with the frozen
benchmark as their reason and real paired remeasurement as the condition for
updating them. This is neither an old-Python fallback nor a claim that newer
packages are defective. The approved migration permits compatible dependency
updates; security fixes remain in force. Keep historical baseline bytes and
matrix binding unchanged, verify original benchmark scenarios now reach their
intended quality/tamper checks, and qualify actual installations in native CI.

Evidence: logs/python314-baseline-probe-lock.txt, python314-baseline-{pypi,osv}.json,
python314-baseline-{x86_64-unknown-linux-gnu,x86_64-pc-windows-msvc,macos15}.txt;
CI jobs125910257141/125910257165/125910257254. Initial broad upgrade remains recorded
in74bd1524; the smaller measured dependency set supersedes its probe versions.

Primary sources and actual current tooling: PSF Python version/packaging contracts,
Astral uv universal constraints and no-build resolution, PyPA pip-audit universal
collector and OpenSSF OSV advisories. See the already researched approved Python
proposal and all-marker audit design; no new package or advisory service is added.
- https://docs.astral.sh/uv/concepts/resolution/
- https://docs.python.org/3.14/library/platform.html
- https://github.com/pypa/pip-audit
- https://google.github.io/osv-scanner/supported-languages-and-lockfiles/
