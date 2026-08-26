# Retiring a supported host

Date: 2026-08-26.
Question: `knowledge/notes/retire-cursor-and-antigravity-decision.md` removed
two IDE hosts at the owner's instruction and kept a removal-only path —
`retired_cursor_hooks_resource` and `retired_antigravity_hooks_resource` — so
that `uninstall` on a machine whose `run/install/manifest.json` still names
`cursor-user-hooks` does not fail closed on
`install_resource_request_mismatch`. How do projects retire an integration
without stranding users who already installed it, and is a removal-only path
sufficient?

## Finding 1 — retirement is three stages, and the middle one is a warning

Homebrew's policy is the clearest statement of the shape, because it names the
stages and attaches a clock to them. A formula moves through **deprecated**
(the action proceeds, the user sees a warning), **disabled** (the action fails
with an error), and **removed** (the code is deleted). Disabled formulae in
`homebrew/core` are removed automatically one year after their disable date, and
the policy requires that "when a formula is deprecated or disabled, a reason
explaining the action must be provided."

Kubernetes' API deprecation policy is the same three stages with longer clocks
and an explicit contract: "a deprecated API will continue to function until
removal (at least one year from the deprecation), but usage will result in a
warning being displayed." GA APIs may be marked deprecated but "must not be
removed within a major version"; beta APIs get three releases or nine months,
whichever is longer; only alpha may be removed "in any release without prior
deprecation notice."

The common structure is not the removal path. It is that **the user is told,
by the software, while it still works**. The warning is the deliverable; the
removal is the consequence.

## Finding 2 — the removal-only path is the part that is usually forgotten

Nothing found treats keeping the uninstall path as optional, and the failure
mode when it is dropped is well documented in the packaging world under a
different name: orphaned configuration. `apt remove` leaves a package's config
files behind; `apt purge` is the separate verb that takes them, and the standard
advice is `apt purge` followed by `apt autoremove --purge` precisely because
"leaving residual configuration files and orphaned dependencies... creates
security debt and operational drift that eventually breaks upgrades."

That is exactly the hazard the decision names for `~/.cursor/hooks.json`: a
handler pointing at a vault nothing maintains, with no supported way to take it
back. Keeping `retired_cursor_hooks_resource` as a reader/writer with no
template, no desired bytes and no recogniser — `write_owned` refusing with
`install_resource_retired` — is the right construction, and it matches how the
same defect was fixed here for the OpenCode plugin on 2026-08-23. The decision's
observation that "subtracting a fragment from a shared JSON file needs the
file's format, so no generic retirement resource is possible" is correct: the
format knowledge *is* the removal path, which is why the code has to survive the
feature.

So on the half that projects usually get wrong, this decision is ahead of the
practice, not behind it.

## Finding 3 — what is actually owed: a notice, and a way to remove one host

Measured against Homebrew's and Kubernetes' shape, the retirement is missing the
middle stage entirely, and on this codebase that has two concrete consequences.

**Nothing tells an affected machine.** The decision removed "the two integration
hosts and their doctor checks in `scripts/doctor.py`", so `doctor` no longer
mentions them. `inspect_install_state` in `scripts/install_control.py` reports
only whether `manifest.json` and `transaction.json` are present, plus the
unowned paths from `_UNOWNED_PATHS`; it does not enumerate the resource ids the
manifest holds. So a machine whose manifest still names `cursor-user-hooks` gets
no signal from any command: capture simply stops, hooks stop being updated and
verified, and the health report is silent about it. Homebrew's deprecation stage
exists to make exactly this visible; Kubernetes' warning exists for the same
reason.

**The removal path is all-or-nothing.** The retirement resources are reachable
only through `uninstall` and `rollback`, which operate on the whole manifest.
There is no `install_control.py` verb that takes back just the Cursor fragment
and leaves the rest of the install standing. The Homebrew analogue would be
`brew uninstall <formula>`; here the only equivalent is removing the whole
installation and reinstalling. For a user who wants to keep LLM Wiki on Claude
Code and only wants the dead Cursor handler gone, the supported route is to
uninstall everything.

Neither is a defect in what was built. Both are things the three-stage practice
would have supplied and this retirement does not.

## Does the decision hold?

The decision holds, with one qualification, and the qualification is about
coverage rather than correctness.

- **Removing at the owner's instruction** is not a technical question and is not
  second-guessed here. The decision records the instruction verbatim and is
  explicit that neither platform was broken.
- **Keeping the removal-only path** is right, is the part most projects omit,
  and the reasoning given for it — fail-closed manifest rebuild, format
  knowledge as the removal path — is sound and matches the packaging world's
  orphaned-configuration experience.
- **Going straight from supported to removed with no notice stage** departs from
  every retirement policy examined. Homebrew and Kubernetes both hold a
  deprecated-but-working state for a year with a warning the user sees. The
  decision's "What an existing user of those platforms loses" section states the
  loss accurately but treats it as a consequence rather than as something owed.

The strongest defence of skipping the notice — and it is a real one — is scope:
no Cursor or Antigravity install was ever exercised on any machine available to
this project, so the population being warned may well be empty. That is stated
in the decision itself. A deprecation clock protects users who exist. What it
costs to add anyway is one line in `doctor` when the manifest names a retired
resource, and that line is what turns silent stranding into a fixable state.

## What this research does not claim

- It does not claim any user is affected. No evidence exists of a real Cursor or
  Antigravity installation of LLM Wiki, here or anywhere.
- It does not claim the removal path is broken. It was not exercised in this
  research; the decision's own evidence is a test that installs a fragment
  through the historical resource shape and removes it with current code, with
  bytes restored exactly, and that is the right test.
- It does not claim `uninstall` is the only reachable route with certainty for
  every code path. It was read, not run: `_owned_resource_factories` supplies
  the retired ids and `_selected_ide_hook_resources` no longer selects them, and
  no per-resource removal verb was found in the CLI.
- It does not evaluate whether a deprecation clock is appropriate for a
  single-operator product. Homebrew's one year and Kubernetes' three releases
  are calibrated to ecosystems with unknown users; this product has one known
  user who issued the instruction.

## Sources

- Homebrew, *Deprecating, Disabling and Removing Formulae* — the three-stage
  lifecycle, the required reason, and automatic removal one year after the
  disable date.
  https://docs.brew.sh/Deprecating-Disabling-and-Removing
- Kubernetes, *Deprecation Policy* — "a deprecated API will continue to function
  until removal (at least one year from the deprecation), but usage will result
  in a warning being displayed", and the per-stability-level clocks.
  https://kubernetes.io/docs/reference/using-api/deprecation-policy/ ·
  https://kubernetes.io/blog/2022/08/04/upcoming-changes-in-kubernetes-1-25/
- Debian/Ubuntu packaging, residual configuration after `remove` versus `purge`,
  and the operational cost of leaving it.
  https://linuxvox.com/blog/linux-apt-uninstall/ ·
  https://khimananda.com/blog/remove-ubuntu-packages-correctly
- This repository, read on 2026-08-26: `scripts/install_control.py`
  (`_owned_resource_factories`, `_active_resource`, `inspect_install_state`,
  `_UNOWNED_PATHS`), `scripts/integration_hook_config.py`
  (`retired_cursor_hooks_resource`, `retired_antigravity_hooks_resource`,
  `_refuse_retired_write`).
