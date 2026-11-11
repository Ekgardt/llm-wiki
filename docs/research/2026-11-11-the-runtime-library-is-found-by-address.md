# The runtime library is found by address

Date: 2026-11-11. Supersedes the shared-library part of
[the configured Python runtime dependency component](2026-10-19-durable-compile-progress.md)
(section "2026-10-20: configured Python runtime dependency component").

## Problem

The compile runtime identity fingerprints the Python shared library when the
build is configured with `Py_ENABLE_SHARED=1`. It used to name that library as
`LIBDIR/INSTSONAME` from `sysconfig`. Two facts, observed on the checker and in
this session, broke that rule:

- The checker's python-build-standalone 3.14 reports `LIBDIR=/install/lib`, the
  build machine's directory. No such file exists, so the identity raised
  `FileNotFoundError` and the compile runtime test failed.
- Both local runtimes (python-build-standalone 3.14.7 and Ubuntu 3.12.3) report
  `Py_ENABLE_SHARED=1` but link the interpreter into the executable:
  `/proc/self/maps` lists no `libpython`, and `dladdr` places `Py_GetVersion` in
  the main program. A configured path names a file the process never runs.

A name-based lookup (`dlopen(INSTSONAME, RTLD_NOLOAD)`, tried in between) also
fails: it cannot tell "not mapped" from "mapped under another name", for example
a macOS image whose install name is `@rpath/...`, so it could silently drop a
library the process really uses.

## Decision

Bind the image that actually holds the running interpreter's code:

1. `dladdr` on the address of `Py_GetVersion` (through `ctypes.pythonapi`, the
   running interpreter's own symbols) gives the holding image's path and base.
2. The main program is recognised by its base address, never by name, because
   `dli_fname` for the main program is only `argv[0]`:
   - Linux: `dladdr` on `getauxval(AT_PHDR)`, the kernel-reported address of the
     executable's program headers.
   - macOS: the address of `_mh_execute_header`, the linker-defined header symbol
     of the main executable, looked up through the `dlopen(NULL)` handle.
   - Any other Unix refuses with an explicit error instead of guessing.
3. Same base: the interpreter is linked into the executable, which is already
   fingerprinted; no library is bound. Different base: that image is the library;
   a path that is not a file is refused. `dladdr` failure is refused.

`Py_ENABLE_SHARED=0` still binds no library, and unknown configuration still
refuses. Windows keeps using the loaded `sys.dllhandle`.

## Alternatives considered

- Configured path (`LIBDIR/INSTSONAME`, `LDLIBRARY`): wrong on relocated builds
  and on shared-configured but statically linked executables. Rejected.
- `dlopen(name, RTLD_NOLOAD)`: presence test by name only; a mismatch reads as
  "not loaded". Rejected.
- Parse `/proc/self/maps` for `libpython*`: Linux only and name-pattern based.
  Kept only as an independent test oracle on Linux.
- `dl_iterate_phdr`: complete on glibc but needs callback plumbing through
  ctypes and does not exist on macOS. `dladdr` gives the same answer for one
  address on both platforms.
- `dli_fname == sys.executable`: `dli_fname` for the main program is `argv[0]`
  (relative, or arbitrary with `exec -a`). Rejected in favour of base addresses.

## Trade-offs and limits

- A statically linked interpreter now binds no library entry. That is correct:
  its code is inside the executable, which the identity already fingerprints.
- macOS behaviour is traced from Apple's documentation and dyld sources, not run
  in this session; the macos-15 CI job runs the cross-platform tests that would
  expose a wrong main-program base.
- Linux was verified here: the main-program base equals the first
  `/proc/self/exe` mapping, `Py_GetVersion` resolves to the main program on both
  local runtimes, and libc's `getpid` resolves to `libc.so.6` outside it.

## Sources

- glibc `dladdr(3)` — https://man7.org/linux/man-pages/man3/dladdr.3.html, and
  glibc `elf/dl-addr.c`, which sets `dli_fname` to `_dl_argv[0]` for the main
  program — https://git.crux.nu/system/glibc/src/commit/165ab1d78d209289a971f121cd3331475b340692/elf/dl-addr.c
- FreeBSD `dladdr(3)`: for the main executable "the pathname returned in
  dli_fname may not be correct", it is taken from `argv[0]` —
  https://man.bsd.lv/FreeBSD-11.1/dladdr.3
- Linux `getauxval(3)`, `AT_PHDR`: address of the executable's program headers —
  https://man7.org/linux/man-pages/man3/getauxval.3.html
- Linux `dlopen(3)`, `RTLD_NOLOAD` is a presence test by name —
  https://www.man7.org/linux/man-pages/man3/dlopen.3.html
- Apple `dladdr(3)`: `dli_fname` is the pathname of the image containing the
  address — https://developer.apple.com/library/ios/documentation/System/Conceptual/ManPages_iPhoneOS/man3/dladdr.3.html
- Apple developer forums: `_mh_execute_header` is the linker symbol for the start
  of the Mach-O image and exists only in an executable —
  https://developer.apple.com/forums/thread/749458; dyld test
  `dlopen-NULL-RTLD_FIRST`: `dlopen(NULL)` is the main-executable handle —
  https://git.saurik.com/apple/dyld.git/blob/HEAD:/unit-tests/test-cases/dlopen-NULL-RTLD_FIRST/main.c
- python-build-standalone behaviour quirks: build-time `/install` paths remain in
  sysconfig data — https://docs.astral.sh/python-build-standalone/quirks/
