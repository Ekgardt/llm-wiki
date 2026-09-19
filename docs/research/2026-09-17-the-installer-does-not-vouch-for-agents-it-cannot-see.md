# The installer does not vouch for agents it cannot see

Dated 2026-09-17. Finding I-B1 of the third audit (medium, suspected; reachable since I-A1
was fixed). The research before the fix.

## What was found

- `--confirm-all-agents-stopped` is the operator's statement that no agent session is
  running against the vault. Offline adoption of Reliability V3 requires it because the
  cutover reads the two legacy operational databases as files, publishes the v3 pair and
  replaces the legacy paths with tombstones.
- Both installers pass the flag themselves for the states `fresh`, `upgrade-required` and
  `partial`. Nothing in the installer looks for a running agent. The backend narrows the
  window (a live `compile.pid` or `maintenance.lock` owner and a leased v2 task both refuse
  adoption) but a hook of a running session that writes between the check and the cutover
  is not excluded.
- The three states are not alike. `fresh` is reported only when neither legacy database
  exists and `run/` holds no legacy evidence (`_inspect_legacy_pair`, `_inspect_fresh`): there
  is no old-protocol file a running agent could be writing, so the statement is true by
  inspection. `upgrade-required` means both legacy databases exist — an earlier install
  whose agents may well be open while the owner reruns the installer. `partial` is an
  interrupted cutover of either kind.

## Practice on this date

- SQLite, "How To Corrupt An SQLite Database File", 1.2: "It is also safe to make a copy of
  an SQLite database file as long as there are no transactions in progress while the copy is
  taking place." and before it: "The backup copy then might contain some old and some new
  content, and thus be corrupt." (<https://www.sqlite.org/howtocorrupt.html>, fetched
  today.) The flag exists to make the first sentence true; a program that cannot observe the
  other writers cannot assert it.
- A consent flag a tool sets for itself is not consent. The same line is already drawn in
  this installer for `--protect-push`: it is applied without asking only to the clone the
  installer itself created, and needs the operator's flag for an existing checkout.

## Options

1. Detect running agents (process names). Platform-specific, misses an agent under another
   name or user, and a false "none found" is worse than a question.
2. Keep confirming for everyone. Leaves the finding as it is.
3. Confirm only what the installer can see for itself (`fresh`); for a vault that holds
   legacy databases, adopt only when the operator passed the confirmation to the installer,
   and otherwise name the command and say why.

## The decision

Option 3. `install.sh` accepts `--confirm-all-agents-stopped` and `install.ps1` accepts
`-ConfirmAllAgentsStopped`; the remote re-execution passes it through. One function in each
installer maps (state, confirmed) to `adopted`, `adopt`, `ask` or `unknown`. A fresh install
still adopts with no question, so issue #17 stays closed. README×3 and the user guide say
when the flag is needed.

Files: `install.sh`, `install.ps1`, `README.md`, `README.ru.md`, `README.zh-CN.md`,
`docs/USER-GUIDE.md`, `tests/test_the_installer_does_not_vouch_for_agents_it_cannot_see.py`,
`tests/test_the_installer_says_what_it_needs.py`,
`docs/research/2026-09-17-the-installer-does-not-vouch-for-agents-it-cannot-see.md`.
