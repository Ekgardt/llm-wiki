# A percent sign in a cron line

Dated 2026-09-17. Finding I-A13 of the third audit (low, confirmed by reading and by a
stand-in run). The research before the fix.

## What was found

- `installer_config.build_cron_command` quotes every path with `shlex.quote`. For the shell
  `%` is an ordinary character, so `shlex.quote` leaves it alone.
- cron reads the line before the shell does: "A '%' character in the command, unless escaped
  with a backslash (\), will be changed into newline characters, and all data after the
  first % will be sent to the command as standard input." (crontab(5),
  <https://man7.org/linux/man-pages/man5/crontab.5.html>, fetched today.)
- So a vault, state root, log or `uv` path with a `%` in it produces a maintenance line that
  is cut at the `%`. systemd values are escaped for their own `%` rule; cron values are not.

## Practice on this date

- cron implementations differ in what they hand to the shell after an escaped `\%`: some
  drop the backslash, some keep it. Outside quotes the shell reads `\%` as `%`, and a bare
  `%` as `%`, so a `\%` placed *outside* single quotes is right under both. Inside single
  quotes a kept backslash would stay in the path. (The first sentence is from memory of the
  Vixie and cronie sources and was not re-read today; the form chosen does not depend on
  which is true, which is the reason for choosing it.)

## The decision

- One function quotes a value for a cron line: the value is split at `%`, each piece is
  quoted with `shlex.quote`, and the pieces are joined with `\%`. A value without `%` is
  rendered exactly as before, so no installed cron block changes.
- The test passes a path with a space and a `%` through both cron behaviours and then
  through a real `sh`, and compares what arrives with the path.

Files: `scripts/installer_config.py`, `tests/test_a_percent_sign_in_a_cron_line.py`,
`docs/research/2026-09-17-a-percent-sign-in-a-cron-line.md`.
