# Permission fixtures check Windows ACLs

The October 7, 2026 Windows CI failures exposed two Unix-only fixture assumptions. `chmod(0644)` does not grant another Windows account read access. A Windows directory's Unix mode bits do not describe its DACL. Neither failure proves that private vector data was exposed.

The growing-database fixture now verifies the existing strict owner-only runtime check, grants Everyone read access to its own fixture database with the existing `icacls` runner, and verifies that the owner-only check rejects it. The original admission error assertion remains. POSIX still uses mode 0644.

The vector-copy fixture retains the original byte equality, distinct copy, truncation, seal and cleanup checks. POSIX still requires directory mode 0700. Windows instead reads the real parent and copied-file security descriptors. The parent must have a protected, non-null DACL; both objects must limit grants to the current verified actor, Owner Rights, SYSTEM and Administrators, with effective owner full control. A child may inherit the parent's grants. This follows the supported Python temporary-directory policy; it does not impose the operational database's stricter single-owner ACE policy on global temporary scratch. Unexpected ACE shapes and broader grants fail the fixture.

No production permissions, dependency, path or runtime contract changes. The native descriptor reader uses pywin32 312, already locked through the required MCP dependency on Windows. It reuses the product's verified token SID rather than guessing an account name. An additional Windows-only control grants Everyone read on its own neutral temporary copy and checks rejection. Linux skips this actual DACL control explicitly. Linux predicate tests and passing POSIX tests are not native Windows qualification.

Primary sources checked October 7, 2026:

- [Python 3.10 os.chmod](https://docs.python.org/3.10/library/os.html#os.chmod): Windows chmod supports the read-only flag rather than Unix access permissions.
- [Python 3.10 os.mkdir](https://docs.python.org/3.10/library/os.html#os.mkdir): supported Windows mode 0700 applies restricted access. CPython 3.10.20 source uses protected SYSTEM, Administrators and Owner Rights grants.
- [Microsoft icacls](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls): grants and numeric SID syntax describe real Windows ACL mutations.
- [OWASP insecure temporary files](https://community.owasp.org/vulnerabilities/Insecure_Temporary_File): creation and effective access controls matter, rather than a portable-looking mode assertion.
- [pywin32 312 security bindings](https://github.com/mhammond/pywin32/blob/b312/win32/src/win32security.i): GetNamedSecurityInfo returns a managed security descriptor and releases the underlying SDK allocation.

Alternatives rejected: weakening the original runtime error assertion, treating mode 0777 as evidence of disclosure, adding a ctypes SDK implementation despite an existing supported binding, or hardening production scratch merely to satisfy an incorrect POSIX-only assertion. Actual Windows CI remains required to verify the native branch.
