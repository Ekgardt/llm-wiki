# Cache race tests establish observable changes

Date: 2026-10-05.

The full point-7 candidate regression produced 11,658 passes, 206 skips and one failure in the existing same-inode cache-race test. The unchanged test passed on the ordinary disk and failed on tmpfs. A diagnostic established that two same-length writes on tmpfs shared both mtime and ctime. Nanosecond fields do not guarantee distinct values on every write.

The fixture now writes genuinely changed content and explicitly changes mtime to an observable value before opening the file. The original expected PermissionError remains. A separate negative test restores the original mtime after changing the cache schema, proves the stat identity matches, and requires cache rejection before the application validator runs. No production reader, identity predicate, or validation guard is changed.

Both full cache-test runs passed: 57 tests on the ordinary disk and 57 on tmpfs. Four changed callables, including the nested callback and lambda, were checked: maximum cyclomatic complexity 3, maximum one if and one conditional nesting level. Python 3.10 grammar and Ruff passed. These results do not close point 7 or qualify unrelated model changes.

Alternatives: sleeping retains dependence on clock granularity; adding ctime alone does not distinguish the observed same-tick writes. Controlled metadata plus independent content validation directly establishes both intended cases. Stat identity alone cannot guarantee detection of every same-size modification with matching timestamps.

Sources: [Python stat results](https://docs.python.org/3/library/os.html#os.stat_result), [Linux inode timestamps](https://man7.org/linux/man-pages/man7/inode.7.html), [Git racy index](https://git-scm.com/docs/racy-git). Local diagnostic and regression evidence: `logs/audit-2026-10-05-step7-cache-stat-clock-diagnostic.json`, `logs/audit-2026-10-05-step7-cache-timestamp-{ext4,tmpfs}.log`, and `logs/audit-2026-10-05-step7-cache-timestamp-complexity.json`.
