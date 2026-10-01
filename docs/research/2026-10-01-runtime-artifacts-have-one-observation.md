# Runtime artifacts have one observation

Research date: 2026-10-01. Python 3.14.6, installed candidate based on 8046f245.

## Cause and full path

MCP doctor starts a separate read-only doctor process with the same ten-second
deadline. Doctor collects health, then validates runtime retention. On the real
vault the complete unprofiled run took 11.097 seconds: health checks about 6.75,
runtime retention about 4.35. The MCP operation timed out before receiving JSON.
A profiler identifies three directory walks of the same transaction artifacts:
database identity lookup, retained/unknown classification, staged-prune lookup.
Across runtime directories 44,862 entry validations ran. Profiler overhead made
its total 26.9 seconds; that is not the normal latency measurement.

The graph shows the validator also serves backup validation. Its result is a
retention observation, never a deletion permit. SQL records, artifact names,
containment, undo receipts and owner checks remain authoritative.

## Sources and alternatives

- [Python os.scandir](https://docs.python.org/3/library/os.html#os.scandir): an
  enumeration observes directory entries; concurrent additions/removals are not
  an atomic filesystem snapshot. Repeated walks do not create such a snapshot.
- [SQLite isolation](https://www.sqlite.org/isolation.html): database readers see
  committed state; this does not make a filesystem walk part of a SQL snapshot.
- [Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/):
  diagnostic cost matters, particularly in loops and critical paths.

All three primary sources were read on the research date. Considered: enlarge the
timeout, return an old report, weaken artifact checks, or remove repeated work.
The last is the smallest change that addresses measured work without masking
errors, changing the time budget or dropping any artifact from inspection.

## Contract

Enumerate and validate the current artifact list once per coordinator validation.
Use its identities for database lookup and the same list for staged/retained/
unknown classification. Each invocation makes a fresh observation; nothing is
cached between requests. Every observed entry retains the original resolution,
containment and kind checks. Concurrently committed rows are read by the later
database lookup; vanished or escaped entries refuse validation. A new artifact
after enumeration belongs to a later observation, as with any directory walk.
No atomic cross-filesystem/SQL guarantee is introduced. Runtime deletion still
requires its existing exclusive/offline protocol and permit remains false.

Old implementation fails the regression with three enumerations instead of one;
other regressions preserve unknown artifacts, interrupted prune, escaping
symlinks, vanished entries and rows committed during the observation. Existing
large-ledger, backup and deletion tests remain required. Real end-to-end MCP
latency after installation is a separate acceptance item, not inferred from
the synthetic regression. The repeated enumeration implementation is replaced;
no runtime switch or second implementation is retained.

The same scan now resolves the state root once and checks it again at completion;
each entry is still resolved against that initial root. Retargeting the root or
an entry escaping it is refused. No cache survives a scan. Additional regression
on old code records three root resolutions for three entries, versus two per
observation now. The full real-vault candidate still needed 10.06–10.63 seconds,
so eliminating redundant work alone does not fit the old generic 10-second call.
The selected host setting is `mcp.doctor_seconds=16`, leaving about five seconds for
load variance beyond the measured complete 11.097-second baseline. Revisit with
history growth, CPU load or corpus changes; no delay is imposed on faster calls.

The child formerly received the exact parent deadline: when a check exhausted
it, even an honest incomplete report could be killed before delivery. Default
`mcp.doctor_return_seconds=1` reserves the report tail (observed 5.239 seconds for
a five-second diagnostic budget). It is configurable and capped at half the
remaining time so short inherited deadlines still permit checks. The parent
deadline and mutation budgets remain unchanged. A separate regression observes
the old identical child/parent deadlines and the new earlier child deadline.
This is deadline allocation, not a sleep or a false successful health result.
