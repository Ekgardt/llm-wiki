# A held provider proof must observe replacement, not lock out the editor

Checked on 2026-10-07. The Windows installer qualification reported six failures:
a replacement attempted inside the held proof failed with access denied before
the stable-identity assertion was exercised. The old CRT descriptor admitted
ordinary read/write sharing but not delete sharing. Windows rename/replacement
needs the latter while another reader is open.

The provider proof now reuses the existing generation catalog's read-descriptor
boundary. On Windows that boundary requests only `GENERIC_READ`, shares read,
write and delete, opens an existing file without following the leaf reparse point,
and makes the transferred CRT descriptor noninheritable. On POSIX its read-only,
binary/no-follow flags match the previous provider opener. No catalog state is
read, no generation is built, and no new reader or native dependency is added.
The binary stream wrapper additionally closes its transferred descriptor if
construction fails.

Sharing is not authority. Every existing held/reopened identity comparison remains:
file metadata, owner/group, full DACL bytes, stable native file identity, whole
content bytes, and fresh path resolution. A replaced or modified proof still
refuses; old bytes in the held handle cannot authorize the new path. The held
handle stays open through the full manifest/transaction/desired proof and is
closed on success or failure. No policy, DLP, persisted record or trust check is
changed.

The POSIX-hosted Windows API model checks the access and share flags, no-follow
request, binary old/new bytes, read-only access, noninheritability and closure.
A separate control detects a leaked descriptor when `fdopen` fails. Both controls
failed against the old opener. This model is not a native Windows result: the
unchanged six native replacement assertions still require real Windows CI.

Alternatives rejected: closing the held file before replacement loses its identity
witness; allowing access-denied in the assertions fails to test replacement;
adding another CreateFile/CRT implementation duplicates an existing boundary.
The metadata-only Windows workspace opener lacks `READ_CONTROL` and cannot replace
the full owner/group/DACL proof. Reusing the generic-read boundary retains that
permission without requesting file write/delete access.

Primary sources checked:

- [Microsoft CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew): share-delete permits rename; access rights and share rights are distinct; reparse-point opening.
- [Python 3.10 msvcrt](https://docs.python.org/3.10/library/msvcrt.html#msvcrt.open_osfhandle): native-handle transfer to a CRT descriptor and binary file object.
- [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html): identity and use must remain tied across concurrent changes.

Existing native code and its ownership/failure controls are reused unchanged.
Actual Linux CPython 3.10.20 results are recorded separately; native Windows remains
unqualified until its real sharing, ACL and replacement gates pass.
