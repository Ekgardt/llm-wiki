# What an update created, its rollback takes back

Date: 2026-09-28. Follow-up to `2026-09-28-an-update-replaces-what-it-owns.md` (law 2).
Scope: `scripts/install_control.py`, `scripts/install_takeover.py`.

## Кратко для владельца

Три доделки к «обновление заменяет своё». Первое: файл-дополнение `50-local.conf`,
который создало обновление, теперь записан как часть этого обновления, и откат
этого обновления его убирает — иначе после отката перенесённая строка стояла бы и в
старом юните, и в дополнении, и `ExecStart` выполнился бы дважды. Чужой
`50-local.conf` не трогается никогда. Второе: копии заменённых файлов в
`run/install/displaced/` живут столько же, сколько резервные копии конфигураций
агентов (настройка `retention.config_backup_days`, 90 дней); копия, на которую
указывает текущая точка отката, не удаляется. Третье: переезд профиля в другой файл
тоже ставит новое раньше, чем убирает старое; только cron и задачи Windows по-прежнему
снимаются первыми — их нельзя прочитать обратно, и установщик говорит это прямо.

## Sources

1. systemctl(1), `daemon-reload`: "Reload the systemd manager configuration. This will
   rerun all generators ..., reload all unit files, and recreate the entire dependency
   tree." A drop-in written or removed is not seen until then, so each drop-in write
   and removal reloads. https://man7.org/linux/man-pages/man1/systemctl.1.html
2. systemd.unit(5): drop-ins "parsed after the main unit file itself" (cited in the
   parent note) — the reason a moved `ExecStart` in a drop-in beside a restored unit
   that still carries it runs twice. https://man7.org/linux/man-pages/man5/systemd.unit.5.html
3. Nix profiles: a rollback "will just make the current generation link point at the
   previous link" — what a generation added is not part of the previous one.
   https://nix.dev/manual/nix/stable/package-management/profiles
4. tmpfiles.d(5), Age: "If a file or directory is older than the current time minus the
   age field, it is deleted." Age-based retirement of disposable copies.
   https://man7.org/linux/man-pages/man5/tmpfiles.d.5.html
5. restic forget: `--keep-last n` keeps "the n last (most recent) snapshots";
   `--keep-within` keeps "all snapshots having a timestamp within the specified duration
   of the latest snapshot". Age retention with a protected recent set.
   https://restic.readthedocs.io/en/stable/060_forget.html
6. Windows Installer Component table: a component is detected by its KeyPath, and "Two
   components cannot share the same key path value" — an installed thing is identified
   by where it is, not only by its name.
   https://learn.microsoft.com/en-us/windows/win32/msi/component-table

## Decisions

- A drop-in the takeover plans is a resource of that update's transaction: written
  after the units (each write reloads systemd), reverted with the transaction on
  failure, recorded as released (not kept in the manifest, so a later update or an
  uninstall never touches it), and removed by the `rollback` of that same generation
  only, first, before the unit is restored. A drop-in that already existed is never
  planned; one changed after the update stops the rollback and is named.
  Alternatives: a separate ledger file written after commit (not transactional; a
  crash between commit and ledger loses the link); a drop-in kept in the manifest
  (the installer would then own the user's file and retire it on the next update).
- Displaced copies reuse `retention.config_backup_days` (90 days, the archive's hot
  window, the same reason the agent-config backups have: both undo an installer
  rewrite). A copy whose digest is referenced by the active manifest or transaction
  (the current rollback point) is kept whatever its age. Retirement runs on every
  install, the only writer of the directory, so the store is bounded by the edits of
  the last 90 days. No count bound: none has a basis.
- Resources are matched by (id, kind, location) inside a transaction, as MSI matches a
  component by its key path; a profile moved to another file is therefore one update
  (new block first, old block removed last, both reverted on failure). A cron table
  entry and a Windows task cannot be read back as they are, so a change that involves
  them keeps the take-back-first path, and the installer says so with the resource,
  its location and the command.
