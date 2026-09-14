# The old provider sessions are removed

Dated 2026-09-14. The remaining part of item 1.2 of `docs/AUDIT-2026-09-14-2.md`, decided
on the owner's delegation («решения принимай самостоятельно после выполнения пяти
правил»). The research before the action.

## What was found

- `docs/research/2026-09-14-a-memory-call-leaves-no-session.md` stopped new copies:
  `claude -p` now runs with `--no-session-persistence`. The copies made before stay in
  `~/.claude/projects/-tmp-llm-wiki-provider-*`.
- Checked on this machine today, reading structure and the `cwd` field only, never the
  text: 11 548 directories, 1.3 GB. Every one holds exactly one `.jsonl` transcript;
  592 also hold an empty `memory/` directory; nothing else. Every transcript's records
  name a working directory under a `llm-wiki-provider-` temporary directory — no
  directory holds a session started anywhere else. None was modified in the last hour.
- They are copies of the vault's private prompts and model answers, outside the
  vault's retention, deletion and backup rules. Nothing reads them: the CLI cannot
  resume them usefully (their working directory was deleted after each call), and
  `backfill_sessions` now skips them.
- The live installation still runs the code from before the fix until the branch is
  merged and the nightly update applies it, so a few new directories can appear until
  then.

## Practice on this date

- Data minimisation and storage limitation: personal data kept longer than its purpose
  needs should be erased (GDPR Art. 5(1)(c) and (e), cited as the principle).
- Before deleting, verify the scope exactly and delete nothing the check did not cover
  (the rule this machine's instructions give for destructive actions).

## The decision

- Remove the directories whose name starts with `-tmp-llm-wiki-provider-` and that the
  check above classified as provider-only and not modified within the hour. Nothing
  else under `~/.claude/projects` is touched.
- Directories created before the fix reaches the live installation are removed the
  same way after it does.

Files: `docs/research/2026-09-14-the-old-provider-sessions-are-removed.md`.
