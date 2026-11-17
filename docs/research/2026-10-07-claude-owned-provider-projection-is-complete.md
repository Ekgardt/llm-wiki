# The Claude owned projection reads the whole installed provider choice

Dated 2026-10-07. This repairs the existing
[whole-provider contract](2026-09-18-the-installed-choice-of-provider-travels-whole.md),
without adding environment names, runtime locations, configuration schemas or trust grants.

The Claude desired projection already writes all selected nonsecret `PROVIDER_ENV_KEYS`.
Its reader owned only the provider and Claude model, in addition to the two installation
roots. A Codex installation therefore wrote model and reasoning correctly, then failed
its own exact verification because the reader discarded them. Both sides now derive their
provider-key set from the same existing constant. Secrets and unrelated environment keys
remain outside the owned projection. The test provider is still not persisted.

A historical four-key manifest does not prove ownership of additional provider keys that
happen to be present in the physical settings file. The ordinary update must still refuse
that mismatch. The existing explicit `--adopt claude-user-settings` operation can record
those current bytes as the rollback point after the operator verifies them. Adoption is
not automatic, and this fix does not rewrite historical manifests or bypass drift checks.
A genuinely four-key installation updates normally; rollback restores its earlier choice.

Research checked on this date:

- [Twelve-Factor Config](https://12factor.net/config) describes configuration as the full
  deployment-dependent behavior. The page was last updated in 2017; it supplies a stable
  principle, not evidence of a new standard or this installation's ownership.
- [Claude Code settings](https://code.claude.com/docs/en/settings) documents user JSON settings
  and their scopes. This supports preserving unrelated user settings; it does not authorize
  this product to overwrite every setting or bypass native trust.
- [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)
  separates secret handling from ordinary configuration. The existing provider list excludes
  API secrets; the fix keeps that list unchanged and tests preservation of foreign keys.

Alternatives rejected: dropping the Claude resource from an update would retire an existing
owned resource; filtering the desired provider bundle would recreate partial installed
configuration; treating a mismatched read as success would hide failed verification.

Qualification uses isolated CPython 3.10 actual writes, install transactions, old-reader
migration, rollback, foreign-root and preimage refusal, and the existing provider controls.
Linux qualification is not native Windows proof. The preceding native Windows provider
suite qualified the unchanged reader/bootstrap controls; this projection change still
needs its own normal CI coverage. Live installation and native hook trust are separate.
