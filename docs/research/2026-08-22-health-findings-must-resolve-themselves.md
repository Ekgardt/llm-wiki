# A health finding must resolve itself (2026-08-22)

## Why this was researched

The owner stated the governing requirement in one sentence: everything must work
automatically, without involving the user. Two findings in this vault contradict
it, and both are red today:

- `transactions (error)` counts a quarantined attempt from 2026-08-22T10:45:41Z.
  Its sources were compiled successfully ninety minutes later under a different
  operation identity, so no retry will ever appear in its own chain. No command
  disposes of quarantine: `prune` takes only `committed` and `discarded` after
  thirty days, `recover` only unfinished states, `transaction-undo` only
  committed ones.
- `capture (degraded)` counts two lost captures from 2026-08-21. Both were
  diagnosed and the causes fixed the same day. The counter only goes down when a
  human runs `--clear`.

Both are the same defect in different clothes: a report that cannot return to
green on its own. This is a change to a health surface, so rule 2 applies.

## What current practice says

Alert systems resolve on the condition, not on the record of it. Prometheus
alerting rules "deactivate on the first evaluation where the condition is not
met", and the Alertmanager is deliberately stateless so a resolution is
delivered even across restarts. The optional `keep_firing_for` clause exists for
the opposite worry — flapping — which is a reason to add hysteresis, never a
reason to require a human to close an alert.

Self-healing guidance says the same thing in operational terms: remediation is
verified by "re-running the same health check that triggered the alert. If yes,
close the alert and log the resolution." The failure mode of not doing this has
a name — alert fatigue — and the recommendation is to keep only findings that
still describe live conditions, because a permanently red signal stops being
read.

Nothing in current practice recommends an operator acknowledgement as the way
back to green. Acknowledgement exists to silence an alert that is still true,
which is a different thing from an alert that has stopped being true.

## What this changes here

A finding must describe a live condition. Two rules follow, both exact and both
computed from evidence the runtime already keeps.

**A quarantined attempt is history when its work happened.** Today the only
exemption is a committed retry in the same chain. That misses the ordinary case:
the refusal is fixed, the compile runs again, and the new attempt legitimately
carries a different operation identity because its inputs or dispositions
changed. The added rule reads the outcome instead of the lineage — every path
the refused attempt meant to *create* was created by a transaction that
committed. An attempt that intended no creation, or whose intended pages were
never written, stays a finding, because in that case something really was lost.

**A lost-capture counter is reported while it is recent.** The counter is
evidence of a real loss and is never zeroed automatically; what changes is that
the *finding* covers the last seven days. A new loss turns the check degraded
immediately; a week without one returns it to green while the totals stay
visible in the details. The manual `--clear` remains for an operator who wants
the counter itself gone.

Neither rule deletes evidence, and neither can turn a live problem green: both
are conditions on what is currently true, evaluated at every run.

## Sources

- Prometheus, "Alerting rules" (deactivation on the first non-matching
  evaluation; `keep_firing_for` for flapping) —
  https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/
- Prometheus, "Alerts API" / Alertmanager statelessness —
  https://prometheus.io/docs/alerting/latest/alerts_api/
- OneUptime, "How to Build Self-Healing Systems" (verification closes the alert
  by re-running the same check) —
  https://oneuptime.com/blog/post/2026-01-30-self-healing-systems/view
- LogicMonitor, "Preventing Alert Fatigue in Network Monitoring and
  Observability" —
  https://www.logicmonitor.com/blog/network-monitoring-avoid-alert-fatigue
- LogicMonitor, "Self-Healing ITOps: Close the Loop From Detection to
  Resolution" —
  https://www.logicmonitor.com/blog/self-healing-itops-close-the-loop-from-detection-to-resolution

## Open question

Whether the seven-day capture window should instead be derived from the capture
cadence of the machine. A vault used once a fortnight would return to green
without ever proving the capture path works again. Left as a fixed window
because the alternative needs a measure of normal cadence that this runtime does
not yet keep.
