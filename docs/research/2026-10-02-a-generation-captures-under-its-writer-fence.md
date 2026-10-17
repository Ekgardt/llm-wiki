# A generation captures under its writer fence

Research and qualification date: 2026-10-02. Python 3.10 compatible; installed
Python 3.12.3, SQLite 3.45.1, local POSIX filesystem. Native Windows execution
has not been qualified in this change.

The installed full corpus collector, after the directory identity repair,
refused an unfenced capture because its eligible source membership changed.
Four passes took 620.990 seconds without producing a stable snapshot. This is
unverified freshness, not index corruption. Adding attempts or a larger size
ceiling would not make cooperating Markdown writers hold still.

The common generation builder already accepts the canonical coordinator for
publication, but did not pass it to `collect_corpus`. The manual doctor repair
also dropped the coordinator from its maintenance guard. The collector already
supports the coordinator's deadline-bounded, reentrant writer gate. This change
connects those existing contracts and passes cancellation into collection.

The gate covers collection only. It is released before generation extraction
and publication, which retain their existing snapshot validation and publication
CAS. A snapshot describes a real earlier moment; subsequent source changes are
revalidated and excluded at query time. The change introduces no runtime root,
configuration, path, persisted schema, actor role, daemon, or retry budget.

Primary sources checked on the research date:

- [Python 3.10 threading](https://docs.python.org/3.10/library/threading.html):
  thread-local state and reentrant ownership belong to the calling thread.
- [SQLite isolation](https://sqlite.org/isolation.html): a stable database read
  does not establish a consistent snapshot of separately mutated Markdown.
- [Linux flock](https://man7.org/linux/man-pages/man2/flock.2.html): cooperating
  writers need the same advisory exclusion boundary. This product uses its
  canonical fenced registry rather than introducing a second filesystem lock.

Alternatives considered: retrying without exclusion repeats the observed race;
adding a second lock would leave existing writers outside it; holding the gate
through the entire build delays writers unnecessarily; removing source validation
would weaken the evidence contract. Reusing the existing gate during collection
preserves a consistent capture and bounds writer delay by the existing deadline.
External editors do not obey the coordinator and can still cause refusal.

Two regressions failed before the fix: discovery ran outside the supplied gate,
and doctor dropped its coordinator. Both pass after the fix. Actual adopted v3
fixtures additionally verify cancellation, failure cleanup, and subsequent writer
admission. The doctor seam test instruments dispatch; it is not a claim that a
stubbed generation is a production success. Installed full-corpus qualification
is reported separately in private runtime evidence.
