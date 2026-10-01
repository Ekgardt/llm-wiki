# An unchanged ACL does not mutate a generation

Research date: 2026-10-01. Runtime: CPython 3.12.10 on Windows Server 2025
in the installer CI; supported Python versions remain unchanged.

The Windows installer reported `generation changed before registration`.
The catalog seals its artifacts, then opens its registration transaction.
Opening the operational database validates its parent directory and reapplies
its inheritable owner ACL. That happens even when the ACL is already correct.
The generation's open-descriptor guard deliberately includes metadata change
time. Reapplying inherited security metadata can invalidate that guard without
changing the generation's contents. The guard is doing its job.

Primary sources, checked on the research date:

- Microsoft documents propagation to existing descendants when an inheritable
  ACL is set, and distinguishes metadata `ChangeTime` from data `LastWriteTime`:
  https://learn.microsoft.com/en-us/windows/win32/secauthz/automatic-propagation-of-inheritable-aces
  and https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info
- CPython 3.12.10 reads Windows file identity and basic information through the
  native handle APIs; portable stat alone is not a reason to discard the extra
  Windows change-time guard:
  https://github.com/python/cpython/blob/v3.12.10/Python/fileutils.c
- Ansible's independently maintained Windows ACL implementation reads and
  compares existing access rules, including inheritance and propagation flags,
  before applying changes:
  https://github.com/ansible-collections/ansible.windows/blob/main/plugins/modules/win_acl.ps1

The shared hardener now avoids a write only after observing the exact expected
explicit owner entry and a protected DACL. Directories must include both
object and container inheritance. A textual owner entry alone is insufficient:
an unprotected DACL could later inherit additional access. Protection is read
through the supported Windows `GetFileSecurityW` and
`GetSecurityDescriptorControl` APIs (available since Windows XP):
https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getfilesecurityw
and https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getsecuritydescriptorcontrol

An unreadable or different ACL takes the existing hardening and verification
path. No permissions are cached, generation guard is removed, mutation is
ignored, retry is added, or timing limit is changed. The only cached object is
the process's one native API binding, following the existing kernel API style.
This applies to catalog, queue, transaction, adoption and archive consumers of
the same hardener. Data formats and saved candidates remain compatible.

Alternatives rejected: ignoring change time weakens mutation detection;
reordering only registration leaves other consumers exposed; unconditionally
rehashing under the catalog writer lock adds work without removing the metadata
mutation; skipping the parent permission check weakens sidecar protection.
The extra read on first hardening is a tradeoff for avoiding repeated writes
and propagation on subsequent opens. A concurrent external ACL change is not
authorized by this point-in-time check; this is not a durable permission grant.

Regression evidence: the real catalog registration path, with only the OS ACL
propagation boundary modeled by POSIX metadata updates, fails on the old code
with the exact reported ValueError and passes with the idempotent hardener.
An additional native Windows regression registers inherited artifacts twice.
Existing content/identity tamper checks remain unchanged. Native Windows
installer acceptance is required separately; a modeled test is not a claim
that native qualification has already succeeded.
