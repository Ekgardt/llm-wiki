# Installer fixtures follow the current Codex renderer

Checked on 2026-10-07. The approved installed-provider transport keeps one shared renderer for hooks and MCP. Even an empty installed provider bundle uses the inline bootstrap; an empty bundle makes environment application a no-op, not argument rendering a no-op.

The first platform CI run exposed an obsolete installer test dispatcher. Its Bash and PowerShell `uv` substitutes recognized `config-state` and `config-replace`, but the installer now also requests `config-block`. They discarded that command or refused it. The fixtures now dispatch all three real commands to the real configuration implementation.

The old “quoted-equivalent” fixture also constructed the former direct-script arguments. Those arguments remain recognized as an owned migration input, but are stale rather than equivalent to the new registration. Positive fixtures now obtain current arguments from the shared renderer, while separate tests retain the distinction between equivalent, owned legacy, and a changed installed provider bundle. Original enabled, quoted-table, byte preservation, section count, conflict and exit assertions remain in place. Structural installer checks bind the shared renderer invocation to its existing shell function, instead of requiring removed inline TOML literals.

An independent CI branch-shape check found a compound ternary in the non-Windows command parser fallback. A guard-clause helper preserves the same quote removal. The existing branching-shape test remains unchanged.

Linux replay can qualify configuration classification and the Python branch guard. It cannot qualify native Git Bash, CMD, or configured PowerShell invocation. Those existing native tests must run on Windows before claiming that platform's installer and shell transport are qualified. No installed configuration, native trust, global setting, provider selection or process contract changes here.

Relevant primary contracts: [Python shlex](https://docs.python.org/3.10/library/shlex.html), [Microsoft CommandLineToArgvW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-commandlinetoargvw), and [GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax). The preceding provider research records the same-process transport and its shell limitations; this change only repairs its test fixtures and a branch-shape violation.


The same Windows CI run exposed a separate native API error: pywin32 312's `PySECURITY_DESCRIPTOR` has no `GetSecurityDescriptorBinaryForm` method. Its documented buffer interface supplies the full self-relative descriptor instead. The stable proof now copies that buffer with `bytes(descriptor)`, retaining the same file handle, owner/group/DACL query flags and full binary identity comparison. No security field or refusal is removed. Portable buffer-contract tests reproduce the old failure and exercise immutable copying and owner/group/DACL changes; a real pywin32 buffer test and the existing actual DACL mutation control remain native Windows obligations.

Primary contracts checked on 2026-10-07: [pywin32 security descriptor buffer documentation](https://mhammond.github.io/pywin32/PySECURITY_DESCRIPTOR.html), [pinned pywin32 312 implementation](https://github.com/mhammond/pywin32/blob/b312/win32/src/PySECURITY_DESCRIPTOR.cpp), [Microsoft self-relative security descriptors](https://learn.microsoft.com/en-us/windows/win32/secauthz/absolute-and-self-relative-security-descriptors), and [Python bytes construction](https://docs.python.org/3.10/library/functions.html#func-bytes). The obsolete method is replaced rather than retained as an alternate path. Linux simulation is not a native Windows qualification.

## 2026-10-07: interpreter identity and project-qualified stream checks

The native Windows failure compared the external `Popen` launcher PID with the
PID executing the target. Python's Windows virtual environments use redirectors.
The regression now records the executing interpreter's PID before the emitted
bootstrap and compares it with the target's PID. A real POSIX redirector reproduces
the old failure and exercises the replacement assertion without inventing a PID.
This checks same-process execution after interpreter admission, not launcher identity.

The configured PowerShell CI traces showed identical direct and bootstrap streams.
Both included uv's warnings because their temporary root was not a project.
The fixture now provides a minimal dependency-free project and an offline lock,
using the already running test environment through uv's existing project-environment
setting. It performs no sync and changes no installed environment. Both original
`--locked` and `--no-sync` arguments and the exact stream/exit assertions remain.
A real uv control checks the warnings outside a project and exact bytes inside one.
The three nightly fixture writes now use bytes to retain their original LF protocol
on Windows; their ordering, source and CAS assertions are unchanged.

Primary references checked on 2026-10-07:

- [Python 3.10 venv](https://docs.python.org/3.10/library/venv.html): Windows redirectors and environment identity.
- [uv project execution](https://docs.astral.sh/uv/concepts/projects/run/) and [project environment selection](https://docs.astral.sh/uv/concepts/projects/config/#project-environment-path): project-aware execution; the fixture never runs sync against the selected environment.
- [Microsoft process creation](https://learn.microsoft.com/en-us/windows/win32/procthread/creating-processes): process and child execution identities.

Linux controls do not qualify native configured PowerShell or Windows ACL behavior.
The unrelated 50 ms symbol-walk deadline failure remains separate and unresolved.
