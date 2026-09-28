# An update replaces what it owns

Date: 2026-09-28. Research only (law 2); no code changed.

## Кратко для владельца

Гипотеза «при обновлении удалить старую версию и поставить новую — тогда не будет
конфликтов» верна наполовину. Она верна для файлов, которые целиком принадлежат
установщику: файлы расписания, плагин, фрагмент в `~/.bashrc`. Их действительно
надо не сверять, а перезаписывать целиком. Но удалить «всё старое» нельзя:

- в той же папке лежит ваша память (`knowledge/`) и рабочее состояние (`run/`) —
  их удалять нельзя никогда;
- `~/.claude/settings.json` — общий файл с Claude Code и вашими правками; удалить и
  записать заново — значит стереть чужие правки;
- между «удалил» и «поставил» есть окно без расписания и хуков, и если новая
  установка упадёт, машина останется без ничего (это уже случалось, поэтому сейчас
  есть откат).

Рекомендую так (вариант E ниже): установщик полностью владеет своими файлами и при
каждом обновлении перезаписывает их целиком, не спрашивая; ваши настройки живут
отдельно, в файлах-дополнениях, которые он никогда не трогает (для расписания —
`*.service.d/*.conf`, туда же переезжает строка с выбором модели); в общих файлах он меняет
только свой кусок; новая версия ставится до того, как убирается старая, и при
ошибке всё возвращается. Для вас это значит: обновление не останавливается на
«файл изменён вручную», а ваши правки не пропадают.

## Question

The owner's hypothesis: on update, remove the old version and install the new one, so
no conflicts or errors remain. The recent failures on the owner's machines: a revert that
refused a file changed outside the installer, a quarantine that blocked every later
command, scheduler units edited by hand (a model variable) that the installer would
neither adopt nor overwrite, a checkout left on a non-default branch, and model weights
installed without their runtime.

## What the product does today

`install_control` records every external resource it owns (profile fragment, scheduler
units, Claude Code settings fragment, Codex hooks, OpenCode plugin) with before/after
digests and refuses to touch a resource whose current bytes differ from what it recorded
(drift). When the requested resource set changes, `_replace_outgrown_install` already
uninstalls the old set and installs the new one, and `_install_or_restore` puts the old
set back if the new install fails ("a failure after a replacement puts the old set back
first", `scripts/install_control.py`). The code itself is updated in place by the nightly
fast-forward of the checkout, which is also the data directory.

## Sources

1. Debian Policy, conffiles: "if the user edits their file, but the package maintainer
   doesn't ship a different version, the user's changes will stay, silently" and "If both
   have changed their version the user is prompted about the problem and must resolve the
   differences themselves." Also: "A package should not modify a dpkg-handled conffile in
   its maintainer scripts."
   https://www.debian.org/doc/debian-policy/ap-pkg-conffiles.html
2. RPM spec: `%config(noreplace)` — "Don't replace a locally modified file"; such a file
   "is preserved as-is on package updates, but the new content from the package is saved
   with .rpmnew suffix for reference"; "A modified %config file is backed up with .rpmsave
   suffix on erase." https://rpm-software-management.github.io/rpm/man/rpm-spec.5
3. systemd.unit(5): drop-in `.conf` files "will be merged in the alphanumeric order and
   parsed after the main unit file itself has been parsed. This is useful to alter or add
   configuration settings for a unit, without having to modify unit files."
   https://man7.org/linux/man-pages/man5/systemd.unit.5.html
4. Windows Installer, RemoveExistingProducts: removing the old product before installing
   the new "is an inefficient placement ... because all reused files have to be
   recopied"; placed after the new install, "if the removal of the old application fails,
   then the installer rolls back both the removal of the old application and the install
   of the new application." https://learn.microsoft.com/en-us/windows/win32/msi/removeexistingproducts-action
5. Nix profiles: "the default symlink is made to point at the new generation" — a step
   that "is atomic on Unix, which explains how we can do atomic upgrades"; rollback "will
   just make the current generation link point at the previous link."
   https://nix.dev/manual/nix/stable/package-management/profiles
6. Martin Fowler, BlueGreenDeployment: "if anything goes wrong you switch the router back
   to your blue environment"; for data, "separate the deployment of schema changes from
   application upgrades." https://martinfowler.com/bliki/BlueGreenDeployment.html
7. The Twelve-Factor App, Config: "strict separation of config from code."
   https://12factor.net/config

## What the hypothesis fixes and what it breaks

Fixes: stale or drifted files the installer wholly owns (units, plugin, profile block)
stop blocking an update, because they are rewritten, not compared.

Breaks or risks:
- The vault directory is both code and data. "Remove the old version" cannot mean the
  checkout: `knowledge/` is the owner's memory and `run/` has a deletion contract.
- Shared files: the Claude Code settings file is also written by Claude Code and by the
  user. Every source above preserves local edits (dpkg keeps and prompts, RPM keeps and
  writes `.rpmnew`); delete-and-rewrite would lose them.
- The gap: removing first leaves no scheduler and no hooks until the new install ends;
  Windows Installer calls remove-first the inefficient placement and prefers installing
  first, removing after, with rollback of both.
- Live sessions write through hooks during the gap.

## Alternatives

- A. Uninstall then install owned resources only: fixes drift in owned files, still has
  the gap and still destroys edits in shared files unless restricted.
- B. In-place transactional update with adopt/replace for drift (today after PR #48 and
  the drift fix): safe, but every hand edit stops the update until someone chooses.
- C. Owned files are rendered wholly and never edited by users; user customisation lives
  in drop-ins/override files the installer never touches (systemd drop-ins; a local
  settings file): updates can always overwrite owned files; user edits survive by design.
- D. Immutable versioned code directory plus an atomic switch (Nix-style generation,
  blue-green), data outside: the cleanest update and rollback for code, but it moves code
  out of the vault — a structural change to the single-directory decision.
- E. Hybrid: C for external owned files, install-new-before-removing-old with rollback
  (Windows Installer's efficient placement) for the resource set, fragment-only edits
  with preserved remainder for shared files, and the checkout update kept as today
  (fast-forward, refuses local edits) since code and data share a directory.

## Recommendation: E

- Deleted/overwritten on every update: files the installer wholly owns (unit files,
  plugin file, its own profile block, its own fragment inside shared files). A displaced
  hand edit is kept as a recorded preimage and named, like `.rpmnew`/`.rpmsave`.
- Kept: `knowledge/`, `run/`, `cache/` (derived, regenerated), user drop-ins
  (`*.service.d/*.conf`), the rest of shared files.
- User customisation lives in drop-ins or a local settings file (for the scheduler's
  model choice: a drop-in; on one machine that line was hand-added to the unit itself on
  2026-09-13 and would move into a drop-in once).
- Order: write the new resources, verify, then retire what the new set no longer owns;
  a failure rolls both back (the existing restore path).
- Code: the checkout stays the fast-forwarded default branch; the installer and doctor
  name a non-default branch or local edits as the one thing blocking the update.

Cost: moderate — the drift fix already lets `--adopt` take the scheduler; E makes
"replace owned file, keep preimage" the default for wholly owned files and adds a
drop-in for the model variable. D would need the owner's decision because it changes the
single-directory layout.

## Needs the owner's go-ahead

Only D (moving code out of the vault). E stays within the current structure.
