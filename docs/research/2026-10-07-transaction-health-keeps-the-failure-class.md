# Transaction health keeps the failure class

Research and qualification date: 2026-10-07.

The transaction health check caught filesystem, SQLite, deadline and validation failures but discarded their exception class. Each became the same unreadable-state report. The installed 900-second inspection therefore cannot establish which of these caused its failure. This change preserves the class for subsequent inspections; it does not diagnose that historical failure or repair its cause.

The existing error status, incomplete-read flag and prohibition on deleting operational history remain unchanged. Exception messages and tracebacks are excluded because they may contain private source content. Four regression cases failed on the original code and passed with the change. The connected transaction suite passed all 56 tests under Python 3.10. The changed function measured CCN4, two if statements and one nesting level; both new test functions also passed the complexity gate and Ruff.

Three independent primary references were checked on this date:

- [Python exception documentation](https://docs.python.org/3.10/library/exceptions.html#TimeoutError) distinguishes timeout and operating-system failures.
- [SQLite result codes](https://www.sqlite.org/rescode.html) distinguish database failure conditions; a Python exception class alone does not identify a particular SQLite result code.
- [OpenTelemetry exception conventions](https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-spans/) distinguish exception type from message and stack trace, including the privacy concern of message content.

Alternatives were retaining the generic report, exposing exception messages, or introducing a new diagnostic subsystem. Preserving only the existing caught exception's class is the smallest useful change. It adds no dependency, setting, directory, runtime database or deletion permission. A later inspection must still establish the actual operational cause. Audit point 7 remains open.
