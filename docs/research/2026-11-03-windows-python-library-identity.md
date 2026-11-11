# Windows Python library identity comes from the loaded module

Date: 2026-11-03. Correction within the approved Python 3.14 qualification.

Native Windows end-to-end job125943374616 on19d78215 passed the previous
fingerprint stage and retained artifact13234815297: ValueError, Python shared-library
configuration is unavailable. _compile_runtime_library assumes Unix sysconfig's
Py_ENABLE_SHARED/INSTSONAME/LIBDIR. The Windows runtime need not expose those keys.
Do not interpret missing configuration as static linkage and omit the DLL.

Use sys.dllhandle on win32 to identify the actual loaded Python DLL, then the
GetModuleFileNameW API to resolve that handle. Add its ABI declaration to the
existing lazy windows_native owner. Reject a missing/invalid Python DLL handle;
propagate native lookup failure. Grow the Unicode buffer on documented truncation,
starting at the conventional MAX_PATH allocation (not an accepted-path limit).
No directory scan, guessed python314.dll name or current-directory fallback.
The existing streaming fingerprint then binds the actual DLL bytes. Unix static
and shared-library rules remain, including refusal of unknown configuration.

Existing synthetic Unix configuration tests must explicitly model a Unix platform;
new tests cover Windows dispatch, missing handles, buffer truncation, high native
handle values, API errors and actual runtime-library fingerprinting on native CI.
The complete compile dependency inventory and all manifest identity checks remain.
This correction identifies the library; it does not claim to inventory transitive
OS libraries or memory-resident code, which remain outside the existing contract.

Three independent primary publishers checked2026-11-03:
- PSF sys.dllhandle, documented Windows handle to the Python DLL:
  https://docs.python.org/3.14/library/sys.html#sys.dllhandle
- Microsoft GetModuleFileNameW, loaded-module path, zero-error and truncation rules:
  https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-getmodulefilenamew
- python-build-standalone distribution documentation, platform-specific artifacts
  and explicit metadata rather than layout heuristics:
  https://gregoryszorc.com/docs/python-build-standalone/main/distributions.html

Alternatives rejected: treat unknown as static, guess by version/prefix, or bind
only the executable. The loaded-module API directly answers the required identity.
No runtime path, product command, dependency, release or live installation changes.
Evidence: logs/native-windows-capture-19d78215.jsonl;
logs/windows-python-library-callers.json and the following regression reports.

Additional source check: CPython3.14.7 sysconfig._init_non_posix itself uses
sys.dllhandle, but its private _winapi.GetModuleFileName wrapper allocates only
MAX_PATH and returns the truncated string without a truncation refusal. Therefore
this caller uses the existing explicit ABI owner with buffer growth rather than
reusing that truncating private wrapper. Checked primary implementation:
https://github.com/python/cpython/blob/v3.14.7/Lib/sysconfig/__init__.py
https://github.com/python/cpython/blob/v3.14.7/Modules/_winapi.c

Related inventory review: Unix/Windows sysconfig directory layouts differ. This
change preserves the existing configured stdlib-root inventory and separate
package inventory. Whether Windows extension-library directories outside those
configured roots require an additional runtime component remains an explicit
follow-up investigation; no claim of complete transitive runtime-library coverage
is made by this DLL-path correction.
