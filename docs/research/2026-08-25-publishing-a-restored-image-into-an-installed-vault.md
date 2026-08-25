# Publishing a restored image into an installed vault (2026-08-25)

## The question

`private_vault_backup.py restore` unpacks a receipt-bound Restic snapshot into
an empty directory and validates it against the manifest. It stops there, so
moving memory to a new machine ends with the operator running `cp -r` by hand —
the one step with no verification, no refusal, and no record. `OPEN-001`/
`OPEN-007` ask for publication into an installed vault.

## What the practice says

The 2026 restore guidance is consistent on three points that matter here:

- **Restore into a fresh target, then validate** — completeness, integrity and
  timing, with the start, finish, exit status and warnings recorded
  ([How to restore from backup][arphost]; [restore testing with AWS Backup][aws]).
  Our restore already does the first half; what is missing is a recorded
  publication step.
- **The target must not be able to overwrite production by accident** — the
  isolation is the point, not a formality ([restore verification process][mydbops]).
- **Treat the restore script as production code**: versioned, reviewed and the
  same script the responder would run ([restore guide][arphost]).

Nothing in that literature suggests an automatic overwrite of a populated
system. The vault products that do publish into a live target (CyberArk's vault
restore) make it an explicit, privileged, offline operation with the service
stopped ([Restore safes or the vault][cyberark]).

## What this vault does

`publish` takes a validated restore staging directory and an installed vault
root, and copies the image into place **only when nothing would be
overwritten**: every destination path must be absent or byte-identical. Anything
else is a refusal that names the first conflicting path. That covers the case
`OPEN-001` actually describes — a new machine — and refuses the case that needs
a human decision.

Publication re-verifies the manifest digest and the whole image before copying,
so an image edited between restore and publish is refused. It does not take the
maintenance fence: a vault with an empty knowledge tree has no nightly pass to
race, and the write itself closes the remaining window by creating each file
exclusively — anything that appeared in the meantime turns into the same refusal
rather than an overwrite.

## What is deliberately not done

No merge, no overwrite, no "force" flag, and no publication into a vault whose
knowledge tree already holds content. Replacing a populated vault stays a
deliberate human act, exactly as the current documentation promises.

[arphost]: https://arphost.com/how-to-restore-from-backup/
[aws]: https://aws.amazon.com/blogs/storage/implementing-restore-testing-for-recovery-validation-using-aws-backup/
[mydbops]: https://www.mydbops.com/blog/how-to-verify-database-backups
[cyberark]: https://docs.cyberark.com/pam-self-hosted/latest/en/content/pasimp/restoring-safes-or-the-vault.htm
