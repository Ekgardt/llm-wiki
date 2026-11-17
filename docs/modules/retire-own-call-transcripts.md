# Retire the transcripts the memory's own provider calls left under `~/.claude/projects`

`scripts/retire_own_call_transcripts.py` and the tests that exercise them most.

Until 2026-09-14 every memory call through `claude -p` was saved by the CLI as a session outside the vault (entry point `sdk-cli`). The flag `--no-session-persistence` stopped new ones; the residue stayed until an operator deleted it by hand on 2026-09-23 (1 082 transcripts, 101 MB, and 113 empty project directories). A transcript is the memory's own when its records name `sdk-cli` and a working directory the memory calls from: the vault, the platform's temporary directory, or the host's job directory. A session someone held (`cli`) is never touched, nor is a programmatic call made from anywhere else. Run by the nightly pass. Research: `docs/research/2026-09-23-the-memory-retires-its-own-residue.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_memory_retires_its_own_residue.py -q`.
