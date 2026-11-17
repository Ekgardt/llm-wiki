# Doctor reads the whole queue

Research date: 2026-10-02. Installed Python 3.12.3 and SQLite 3.45.1;
implementation remains compatible with Python 3.10. Native Windows execution
is not claimed by this change.

The installed queue held 14,775 tasks, including 12,431 succeeded, 2,294 ready,
26 dead, 23 cancelled and one leased. Doctor selected at most 10,000 task rows
and retained them all in a Python list. It counted side-table records and owners
under the same ceiling, and modern runtime artifact directories under another
10,000-entry ceiling. None of those cardinalities establishes a resource or
corruption boundary.

Six regressions reproduced the consequences: incomplete counts, an old real
metadata defect not found, source failure/fence counts stopped early, empty owner
rows called unknown by number alone, and retained result files not fully counted.

The task query now streams its rows. Ordinary payload rows are not retained in
Python; only the existing result-reference facts and a four-field metadata
projection of dead tasks and answering redrives are kept for validation. This
preserves the dead-task lineage rule without repeatedly scanning a changing
queue. Source failures and fences use SQLite COUNT. Owners are streamed, and
modern result/quarantine artifacts are counted and checked one entry at a time.
The existing caller deadline applies to task iteration, owner iteration and
result-file validation; SQLite retains its existing progress callback. Directory
budget exhaustion or an unsafe entry remains unknown, and a real metadata defect
remains an error. Every retained task, result, source failure/fence or live owner
continues to protect runtime state from deletion.

Primary sources checked on the research date:

- [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html):
  cursors support row iteration without collecting the result list.
- [SQLite progress handler](https://sqlite.org/c3ref/progress_handler.html):
  execution can stop when the caller's existing deadline is reached.
- [Linux readdir](https://man7.org/linux/man-pages/man3/readdir.3.html):
  directory entries can be read sequentially rather than materialized together.

Alternatives: raising the ceiling recreates the failure at a later size;
configuring it would still leave an unsupported truth boundary; fetching every
payload removes truncation while unnecessarily increasing memory; separate
repeated task scans can disagree when another process completes a task. The
chosen stream retains only facts needed by the existing diagnostic rules.

This change adds no configured limit, schema, directory, environment contract or
runtime actor. The retired JSON queue's bounded diagnostic listing remains a
separate unsupported legacy path. Other doctor table/archive/transaction-artifact
limits remain outside this change and are not declared justified or closed.

Additional protections cover an expired deadline without a corruption allegation,
a bad older result hash, an escaping result reference, and a dead task answered by
its retained redrive. Installed queue qualification and measured before/after
costs are separate runtime evidence; the audit remains open until those and the
other outstanding requirements pass.

Measured task-only comparison on the same private SQLite online-backup snapshot
(47,206,400 bytes; online backup 0.141 s; no raw payload printed):

| Implementation | Rows judged | Wall time | Python peak |
| --- | ---: | ---: | ---: |
| Installed capped scan | 10,000 of 15,275 | 0.255 s | 16,248,454 bytes |
| Candidate stream | 15,275 of 15,275 | 0.327 s | 4,032,314 bytes |

The stream also counts the 26 dead and 23 cancelled rows omitted by the old scan.
Retained redrives answer all 26 dead rows in this snapshot; their presence is not
an unresolved-failure allegation. These costs exclude full artifact validation.
The ten new cases pass; related and mandatory checks: 325 passed, three skipped,
69.50 s. No native Windows or complete current-suite result is inferred from them.
