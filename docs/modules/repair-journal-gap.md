# Append a committed sequence the journal does not carry

`scripts/repair_journal_gap.py` and the tests that exercise them most.

`ProjectJournalRebuildRequired` says in its own docstring that append-only ordering needs an explicit verified rebuild, and until now there was none. A sequence the coordinator calls committed while the journal ends before it stops every later sequence for that project, permanently.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
