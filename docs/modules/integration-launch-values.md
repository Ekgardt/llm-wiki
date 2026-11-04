# Installed launch values

The owner holds the existing nonsecret provider keys, payload validation and
canonical base64 transport, exact Python bootstrap, launch-bundle recognition and
platform-specific command quoting. `codex_hook_identity` belongs to the same owner:
recognizing a generated command no longer imports installation configuration.
The legacy script/tail recognition rule is unchanged. Base64 is not encryption.

`integration_hook_config` keeps direct aliases, including private compatibility
names and callable serialization paths. Its `codex_launch_args` remains there
because the omitted-bundle case reads selected provider settings. Environment IO,
model selection, template rendering and installation mutations remain outside this
owner. The owner does not install, start a process, or rewrite configuration.
Its emitted program preserves original-process execution, stdin/output/errors,
import policy, target validation and whole-bundle fallback. Existing native Windows
`win32api` use remains lazy; this is not a stdlib-only module.

Aliases are needed by current installer/doctor/hook integrations and existing
imports. Remove them only after supported consumers migrate and serialization
compatibility is explicitly retired. There is one implementation, not two.

## Research — 2026-11-04

[Python shlex](https://docs.python.org/3.14/library/shlex.html) restricts its shell
quoting guarantee to Unix. [Microsoft CommandLineToArgvW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-commandlinetoargvw)
documents distinct quote/backslash parsing. [RFC4648](https://www.rfc-editor.org/info/rfc4648/)
defines canonical base64. Preserve existing platform branches and exact emitted
bytes. Universal shlex, a new parsing dependency or subprocess trampoline would
change the established platform/process contract without solving the import cycle.
No dependency upgrade, new limit or provider setting is introduced.

Full original implementation definitions and generated body are checked against
pre-transfer baselines. The boundary test recognizes an actual command while
blocking installation imports. Existing integration tests remain at their paths:
`test_codex_installed_provider_bundle_reaches_launch.py`,
`test_codex_provider_bootstrap_preserves_runtime_choice.py`,
`test_codex_provider_bundle_manifest_proves_desired.py`,
`test_codex_capture_hooks.py`, `test_the_installed_choice_of_provider_travels_whole.py`
and `test_claude_provider_projection_round_trips.py`. Their installer and native
process assertions remain mandatory; they are still counted by the whole-project
inventory and are not claimed as a finished integration packet here.

Qualification requires native Windows CI, actual function complexity including the
emitted program, unchanged original assertions, measured weights and complete
packet reconstruction within 100000 tokens. Packet qualification is not deployment
or whole-project completion, nor evidence of full-cycle LLM savings.

## Canonical structural contract

`llm_wiki.integration_launch_values` owns the existing nonsecret provider payload,
exact emitted bootstrap, command parsing and platform quoting. Together with
`codex_hook_identity`, it recognizes owned commands without importing installation
configuration. `integration_hook_config` retains direct compatibility aliases and
its environment-reading launch wrapper; installation/configuration IO stays there.
The lazy native Windows dependency, legacy matching and emitted bytes are preserved.
Complete source, baseline and boundary gates: [module guide](integration-launch-values.md).
