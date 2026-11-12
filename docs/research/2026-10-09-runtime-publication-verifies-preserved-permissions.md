# Runtime publication verifies preserved permissions

Date: 2026-10-09. Status: related local qualification; actual Windows timing qualification remains open.

Windows CI for 6049a215 reported a prompt hook taking 5.0597768 seconds against its existing 5-second host limit, and a separate access-frontmatter export timing out at its transaction boundary. Investigation found redundant work in the shared runtime publisher: it hardened the private staged file, published it in the same directory, then hardened the destination again. Each Windows hardening performs an `icacls` modification and a separate readback. The duplicate modification also concealed permission drift injected after publication: the old implementation rewrote the broader mode and reported success.

The publisher now performs initial hardening once, then checks the destination without changing its permissions. Windows uses the identical strict explicit-owner/full-control/no-other-entry/no-inherited-entry predicate from initial hardening; the listing and predicate are shared, not copied or cached. Non-ASCII owner decoding, subprocess errors, missing rights, additional entries and inherited grants remain refusals. POSIX verifies the exact requested mode. The existing same-parent local-file publication, write-through/flush, hash and identity checks stay in place.

A separate new regression exposed an existing POSIX defect for an explicitly requested owner-read-only mode: `fsync_file` unnecessarily opened the file for writing. POSIX now uses a read descriptor; Windows retains a write-capable descriptor because its flush API requires it. File and directory sync are not omitted.

## Evidence and limits

Initial causal tests: four failed, three passed, one skipped. One failure was the pre-existing read-only sync defect, rather than the redundant hardening. After correction, original runtime/breadcrumb/capture/access guards passed; negative ACL checks additionally preserve all previously forbidden entries. Actual changed-callable measurements enforce CCN at most 5 and the same if/nesting thresholds. Actual Windows owner-only publication has a native-platform guard, skipped on POSIX.

The change removes one Windows permission modification subprocess per published artifact, while retaining its readback. This is a measured reduction in operations, not proof that either observed Windows deadline failure is resolved. The unchanged original deadline tests and complete real platform CI must provide that evidence. No timeout, security rule, native test assertion, runtime location, dependency or numerical cap is relaxed or added. Unexpected permission drift now stays visible for explicit repair.

## Research and alternatives

Checked on 2026-10-09: [Microsoft MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw) distinguishes same-volume moves from copying across volumes, which changes the security descriptor; the product already requires one local parent and does not request cross-volume copying. [Python rename/replace](https://docs.python.org/3.10/library/os.html#os.replace) defines the existing replacement operation. The independent [Linux rename manual](https://man7.org/linux/man-pages/man2/rename.2.html) describes preserving the renamed file and existing descriptors. These justify verifying the resulting object instead of silently rewriting permissions again.

For flushing, the [current Linux fsync manual](https://man7.org/linux/man-pages/man2/fsync.2.html) distinguishes valid descriptor requirements from obsolete writable-descriptor restrictions; [Apple's fsync contract](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html) covers flushing data and attributes and notes separate stronger full-flush guarantees; [Microsoft FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers) explicitly requires write access. This patch preserves the existing durability strength and does not promise stronger power-loss guarantees.

Increasing host deadlines, ignoring the timing assertions, skipping ACL reads or reusing an unverified cached permission approval were rejected. Replacing the entire ACL subsystem would be larger than the confirmed redundant operation and is not necessary for this correction.

Source: CI run 37946903350; private Windows job logs; `tests/test_runtime_publication_verifies_instead_of_rehardening.py`; unchanged runtime, Markdown transaction, capture and access-export tests.

The read-only verifier exposed a related pre-existing identity defect: Markdown transactions and queue permission readers matched the owner's name as a substring. Four causal regressions demonstrated that a sole full-control entry for a different, similarly named principal was accepted. Both readers now share an ACE-principal boundary match; initial hardening, final verification and code-page selection use it. The original non-ASCII-owner tests remain unchanged. Related Linux checks passed (188 passed, one native-Windows case skipped); actual Windows qualification is still required.
