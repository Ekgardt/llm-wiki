# A changed file is named, and a changed schedule can be taken over

Superseded in part on 2026-09-28 by `2026-09-28-an-update-replaces-what-it-owns.md`: a
systemd or launchd schedule is no longer adopted, an update replaces it and keeps the
edited copy. The named refusal below still holds for shared files.

Date: 2026-09-28. Scope: `scripts/install_control.py` drift refusals and `--adopt`;
`scripts/doctor.py` scheduler advice.

## What happened

- Claude-27 (owner's machine), after PR #48: `install_control.py rollback` cleared the
  quarantine, then `install.sh` stopped with `install control failed: install_resource_drift`.
  The message named no file, so the owner could not know what to adopt.
- Claude-30 (this machine, read only): both `llm-wiki-*.service` units carry
  `Environment="MEMORY_CLAUDE_MODEL=claude-sonnet-5"`, a line the installer never rendered.
  It was added by hand on 2026-09-13, on the owner's instruction, and recorded in
  `docs/research/2026-09-13-the-pipeline-asks-sonnet-by-default.md` ("Configured on this
  machine, outside the repository: ... one `Environment="MEMORY_CLAUDE_MODEL=claude-sonnet-5"`
  line each"). No product component writes unit files outside install_control; this is a
  manual edit, not a product defect. The recorded preimage (2026-08-22) lacks the line and
  the time limit this release renders, so the units match neither the recorded nor the new
  rendering. The reader answers `install_scheduler_projection_conflict`, and PR #48's
  `--adopt` refused every scheduler kind. doctor meanwhile says "rerun the installer": a
  deadlock between the advice and the installer.

## Sources

- systemd.unit(5): drop-in `.conf` files "will be merged in the alphanumeric order and
  parsed after the main unit file itself has been parsed. This is useful to alter or add
  configuration settings for a unit, without having to modify unit files." and "Drop-in
  files under any of these directories take precedence over unit files wherever located."
  https://man7.org/linux/man-pages/man5/systemd.unit.5.html
- Debian Policy 10.7.3: "local changes must be preserved during a package upgrade".
  https://www.debian.org/doc/debian-policy/ch-files.html
- rpm-spec(5): "A locally modified %config(noreplace) is preserved as-is on package
  updates, but the new content from the package is saved with a .rpmnew suffix", and when
  a packaged file replaces an existing one "the original file is saved with .rpmorig
  suffix". https://rpm-software-management.github.io/rpm/man/rpm-spec.5

## Decision

1. Every drift refusal names the resource id and locator and the next command: for an
   adoptable resource `rerun the installer with --adopt <id>`; for one that cannot be read
   as it is (cron, Windows Task Scheduler) the safe manual path. The recorded error code
   stays the bare code, so transaction records and tests keep their contract.
2. The systemd and launchd units are the installer's rendered files, the equivalent of a
   package's vendor unit: local changes belong in drop-ins (systemd.unit(5)). With the
   operator's explicit `--adopt`, the installer reads the units as they are, records them as
   the rollback point (the `.rpmorig` of this installer: `rollback` restores them), writes
   its own rendering, and prints every line of the old units the new ones do not carry,
   with where to keep it (a drop-in, or the provider variable exported when the installer
   runs, since the renderer persists provider settings). Nothing is lost silently; the
   Debian rule is kept by the rollback point, not by refusing forever.
   Rejected: merging edited units line by line (guesses at intent); keeping the edited
   units and skipping the update (the release's time limits would never arrive — the very
   thing doctor reports); allowing `--adopt` without the report (silent loss).
3. doctor's advice names the adopt command for units that differ from every rendering, so
   the advice and the installer agree.
