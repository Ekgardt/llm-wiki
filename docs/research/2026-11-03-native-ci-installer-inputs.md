# Native installer qualification inputs

Date: 2026-11-03. Actions run 41609824640 failed in Linux/Windows model selection
and macOS production dependency resolution. This work stays in the candidate.

Linux/Windows already set MEMORY_LLM_PROVIDER=fake, the supported explicit test
provider. The chooser nevertheless tries to select a real model and rejects the
fake adapter. The default select command should report test-provider mode without
calling a provider or saving settings. Explicit model/reasoning requests still
refuse; an explicit real provider continues normal protected verification. Existing
saved settings remain byte-identical. This does not add fake to persistable models.
Do not skip the chooser or manufacture successful verification.

The macOS runner selected Python 3.15 implicitly. The locked onnxruntime 1.27.0
has no compatible cp315 wheel or source distribution according to uv's native log.
Install and pin Python 3.13 in this CI job before changing HOME, as the existing
installer unit job does. Preserve the separate 3.10–3.14 runtime matrix. This is a
reproducible test environment, not a production Python upper bound or a claim of
3.15 support. Expanding that qualification needs compatible upstream artifacts.

Primary sources checked today:
- https://docs.astral.sh/uv/guides/integration/github/ — install Python explicitly;
  project pinning controls interpreter choice.
- https://onnxruntime.ai/docs/install/ — supported binary installation and separate
  source-build instructions; no implicit build promise for an unavailable wheel.
- https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/
  — Python, ABI and platform tags determine binary compatibility.

Alternatives: leaving runner defaults makes the test vary with image updates;
changing all production interpreter constraints overstates this evidence; skipping
model selection loses coverage of a real installation step. Keep the existing
boundaries and add refusal/preservation/provider-dispatch regression tests plus a
workflow ordering invariant. Native end-to-end success still requires a fresh run.

Local red: six failures before the correction (five fake-selection cases and the
missing interpreter pin), with the explicit real-provider refusal case already
passing. Corrected related suite: 106 passed, 13 skipped on Linux Python 3.12.
The skips are existing platform-specific installer tests, not new exclusions.
All changed chooser functions and new test functions pass measured CCN <=5 and
shape checks; the previous provider-selection body is retained under a new name.
Native Windows/macOS execution remains required.
