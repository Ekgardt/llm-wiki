# When a session record leaves the hot tree (2026-08-25)

## The question

Every session leaves a redacted copy of itself under `knowledge/raw/sessions/`.
Measured today: 236 records, 11 MB, about a megabyte a day. Nothing ever moves
them. Should they age out, and to where?

## What the practice says

Retention guidance in 2026 is uniform on one point: match the storage tier to
the access pattern, and move between tiers automatically rather than by hand.
The common shape is hot for the window where data is queried constantly, warm
for occasional investigation, cold for compliance, with lifecycle policies doing
the moving ([Log retention policies][ground]; [How to configure log retention
policies][oneuptime]; [Log archival strategies][archival]). The typical hot
window in those write-ups is 30 days, warm 30–90.

The second point is equally uniform: **archival is not deletion**. Evidence is
preserved in tamper-resistant storage, and a legal hold stops any lifecycle
action ([Retention, archival security and compliance][hardening]). Cost is the
usual motivation there; here it is not the motivation at all — a megabyte a day
costs nothing. What it buys us is a smaller active tree for the readers that
walk it, and an explicit boundary between "what the consolidation pass reads"
and "what the vault keeps".

## What this vault does

- The hot window is **90 days**, the same window the archive contract already
  gives every other hot artifact, rather than the 30 the observability write-ups
  favour: the consolidation pass reads yesterday, but a person looking for how
  something was decided reaches back weeks.
- A record older than that moves to
  `knowledge/raw/sessions/archive/<YYYY-MM>/<date>/`. The bytes are unchanged,
  the file keeps its name, `grep` finds it one directory deeper.
- Nothing is deleted, ever, and there is no compression tier — the Stage 2
  contract forbids a gzip tier, and the saving would be pointless here.
- The move runs inside the same Markdown transaction machinery as every other
  automatic writer, so an interrupted archive is recoverable rather than a half
  moved day.
- It runs in the weekly pass, next to the page archiver that already applies the
  180-day rule.

## What is deliberately not done

No second index, no cold tier, no lifecycle daemon. The vault has one archive
idea already; this is the same idea applied to a directory that had been
exempted from it by accident rather than by decision.

[ground]: https://www.groundcover.com/learn/logging/log-retention-policies
[oneuptime]: https://oneuptime.com/blog/post/2026-01-25-log-retention-policies/view
[archival]: https://oneuptime.com/blog/post/2026-01-30-log-archival-strategies/view
[hardening]: https://www.systemshardening.com/articles/observability/log-retention-archival-security/
