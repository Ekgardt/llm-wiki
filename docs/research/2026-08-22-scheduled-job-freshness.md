# Freshness of a scheduled job is an interval, not a calendar day (2026-08-22)

## Why this was researched

The scheduler check calls nightly maintenance stale unless it ran "today":

    if status in {"ok", "success"} and last_date == today: ok
    else: degraded "Nightly maintenance is stale."

The nightly runs at 03:00. Every night from midnight to three — an eighth of
every day — a healthy timer is therefore reported degraded. Measured on this
machine at 2026-08-22T00:06Z: `systemctl --user list-timers` showed the last run
2026-08-21 03:00 and the next in 2h53m, `last_nightly_status: success`, and the
doctor said degraded at that same moment. The rule lives in `scripts/doctor.py`,
a health surface, so rule 2 applies before changing it.

## What current practice says

Freshness monitoring is expressed as an interval with a buffer, not as a
calendar boundary. The common shape is `now() - last_successful_finish >
schedule_interval * N`, which "catches problems within hours instead of weeks".
Guides on freshness SLAs make the failure mode explicit: thresholds set tighter
than the real load window "trigger constant failures and alert fatigue", and the
fix is to align the warn and error thresholds "to real pipeline schedules plus
buffer time". A worked example for a nightly backup uses 26 hours rather than 24,
"a bit longer than 24 hours because backup jobs sometimes run late from day to
day".

Nagios calls the same idea freshness checking: a passive result older than a
declared threshold is what marks a service stale, and the threshold is a
duration.

Nothing in current practice recommends a calendar-day comparison, and the
midnight discontinuity this vault hit is exactly why.

## What this changes here

The nightly records `last_nightly_at`, an ISO timestamp, beside the date it
already writes. The scheduler check calls maintenance current when that
timestamp is younger than 26 hours — a day plus two hours of slack for a run
that starts late or takes long. State written before this change has no
timestamp, so the old date comparison remains as the fallback and nothing about
an upgraded vault becomes noisier.

A failed last run stays an error and an unknown status stays skipped; only the
meaning of "stale" changes.

## Sources

- Nagios Core documentation, "Service and Host Freshness Checks" —
  https://assets.nagios.com/downloads/nagioscore/docs/nagioscore/3/en/freshness.html
- Paradime, "dbt Source Freshness: Best Practices" (align warn/error to the real
  schedule plus buffer) —
  https://www.paradime.io/guides/blog-dbt-source-freshness-best-practices
- Tacnode, "Stale Data: Causes, Detection, and How to Set Freshness SLAs" —
  https://tacnode.io/post/what-is-stale-data
- Nathan Broadbent, "Kubernetes CronJob Freshness Monitoring with Prometheus" —
  https://world.hey.com/nathan/kubernetes-cronjob-freshness-monitoring-with-prometheus-7a32cbb0

## Open question

Whether the check should read the installed schedule instead of assuming a daily
interval. It would be exact, but it would make the health surface depend on the
scheduler backend it is meant to observe, so the buffer is used instead.
