# Native test fixtures must establish the contracts they exercise

Date: 2026-11-03. Evidence: native run 41625731881 at 3149d5d0, jobs
125882475965/125882476053 (Windows 3.13/3.14 shard 2) and 125882476030
(Windows 3.12 shard 4). These are actual native failures, not inferred from skips.

Fingerprint fixtures create files with chmod(0600), then request owner-only reads.
Windows chmod does not establish an owner-only ACL. Eight cases stop at the real
permission gate before their intended mutation/read checks. Establish fixture
permissions with the existing runtime hardener and assert owner-only validation;
keep security checks, source bytes, mutation callbacks and all result assertions.
A large byte parameter also becomes a huge pytest node ID, overflowing Windows'
environment variable limit before the test runs. Explicit semantic case IDs keep
all original data, including the multi-chunk input, and permit normal collection.

The installer fixture puts an extensionless shebang script named uv on PATH.
PowerShell's native command lookup does not select that script; no calls.jsonl is
created, so nine assertions fail to reach the intended adapter. Use a test-only
shell function in each native shell that invokes the existing interpreter and
shim file explicitly with the complete argument array. It must preserve stdout,
stderr and return status. Real-mode shim execution should use subprocess.run and
propagate its result, ensuring the child completes before inspection and preserving
Windows argument quoting. Native Bash's old real-mode result said WARNING:0 but
left upgrade-required; the missing diagnostic does not establish its exact cause.
Explicit waited child execution is qualified through existing real migrations and
new subprocess failure/argument tests; do not call that native defect closed until
Windows reruns it.

Alternatives rejected: skip native cases, relax owner-only checks, reduce payload
size to shorten IDs, change expected upgrade results, or let tests reach the actual
uv binary unintentionally. Production runtime behavior and security remain intact.
Windows E2E processor_exception is separate and still lacks its underlying reason;
the approved Python migration added failure-artifact retention to expose it on the
next run. No timeout increase or ignored failure is part of this work.

Primary sources checked 2026-11-03, three independent publishers:
- PSF Windows chmod semantics and subprocess argument/return-code handling:
  https://docs.python.org/3.14/library/os.html#os.chmod
  https://docs.python.org/3.14/library/subprocess.html
- pytest parameter IDs and parameterization:
  https://docs.pytest.org/en/stable/how-to/parametrize.html
- Microsoft PowerShell command precedence:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_command_precedence

The next Windows shard also exposed implicit cp1252 fixture writes, a simulated
Git path containing native backslashes, and replacement rejected by Windows before
the source identity assertion ran. Write intended Unicode fixture text as UTF-8
and model Git paths with as_posix(). For replacement cases reuse the catalog's
existing FILE_SHARE_DELETE reader for exactly the fixture path through test-only
support; keep the original exception and content assertions. Separately test that
the unmodified Windows descriptor refuses replacement. This tests both OS refusal
and the application's replacement guard without changing production sharing or
copying native ABI code. The built-in graph is partial; direct source confirms the
reader's handle conversion, noninheritance and cleanup behavior.


Packet measurement rejected the first helper design: importing generation_catalog
pulls the complete executor closure into the bounded-IO owner. The final test
helper instead uses the already qualified windows_native ABI declarations and a
small fixture handle constructor. This creates no second production reader or ABI
declaration. Windows owns the created handle until open_osfhandle transfers it;
failed transfer closes it. Include this helper and Windows ABI source/guide in the
bounded-IO packet; never omit the dependency or exempt it from measurement.

Qualification on Linux/Python 3.14: 203 related checks passed, 11 skipped
(PowerShell/native Windows); common module checks passed 1985, one skipped.
All changed Python functions passed actual CCN and shape analysis; repository Ruff
passed. All 85 maintenance packets reconstruct exact paths/bytes and fit 100000
tokens. Windows results remain pending a fresh run; the older CI run tests 3149d5d0.
