# Portable source inspection and maintenance manifest paths

Date: 2026-11-03. Completed Windows shards exposed remaining non-JSON source
reads using cp1252 and native backslash paths where the manifest contract/tests
use POSIX relative names. This is additional evidence beyond the earlier JSON fix.

Jobs 125828626005/125828626190 fail source inspection in the SQLite compatibility
and contract-guide tests, five record-fixture omissions fail list.remove, and
missing-context regex checks cannot match backslashes. The real refusal happened;
the diagnostic and fixture names were not portable.

Use explicit UTF-8 for source/guide/fixture reads in the module and compatibility
qualification files. Extend the existing AST encoding guard to those files.
Normalize relative manifest/diagnostic names with Path.as_posix through one helper
used by measurement and the record test fixture. Keep native Path objects for IO.
No missing-input check, assertion or source is removed. Verify PureWindowsPath
and PurePosixPath produce identical names without pretending Linux is Windows.

Sources: native job logs; Python pathlib Path.as_posix contract at
https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.as_posix and the
encoding research in 2026-11-03-native-ci-json-encoding.md. Related ASCII-default
source inspection is reproduced locally before edits. Native matrix rerun remains
required; these corrections do not by themselves explain every Windows failure.

The AST encoding guard first failed on the remaining source reads; the real
ASCII-default guide/compatibility run had 16 failures and 160 passes. After
46 explicit UTF-8 additions across 12 files (AST otherwise identical), all 176
pass under that same default. Two PureWindowsPath/PurePosixPath tests failed
before the common relative-name helper and pass afterwards. Related qualification
passes 499 cases. An initial new guard had measured CCN 6; extracting its AST
read detector preserves every assertion and brings the functions within the limit.

One additional Windows 3.10 host-deadline test observed 5.735 seconds against the
unchanged 5-second host contract (job 125828625986). Its abbreviated assertion
attributes 5.7 seconds to a post-tool follow-up whose patched project lookup is
trivial; the available output does not establish whether scheduling or a hidden
operation consumed that time. No timeout, skip or production behavior is changed.
The existing phase diagnostics now retain full JSON and per-thread CPU time so a
native recurrence supplies evidence. This latency failure remains unconfirmed.
