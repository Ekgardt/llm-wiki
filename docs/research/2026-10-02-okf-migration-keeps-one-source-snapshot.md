# OKF migration keeps one source snapshot

Research date: 2026-10-02. Two synthetic regressions reproduced a migration plan whose content differed from the bytes named by its precondition, and refusal of a valid page just above16MiB despite the existing64MiB Markdown transaction target budget. This does not establish a live loss of such a large owner page.

The migrator read bytes to hash them, then separately read text to transform it. An external edit between the reads let the plan describe a different source. The normal CAS boundary refused a changed live preimage, but the plan itself did not bind its text to the recorded snapshot; an edit back to the hashed preimage could permit stale transformed content. The producer now obtains a single stable bounded byte snapshot, decodes and hashes those exact bytes, and builds the migration from that text. The existing CAS writer still refuses a later source change.

The separate16MiB migration limit and unbounded second text reader are removed. Reads use the existing Markdown target budget, and the final encoded output remains subject to that same writer budget. A source that fills the whole budget may be refused when frontmatter makes the output larger; it is not truncated. Existing64MiB optimality is not claimed, and the broader limit audit remains open. Editorial files are skipped before reading; invalid UTF-8 and stable-read errors remain explicit.

Alternatives: increasing only the migration constant retains duplicate reads and source ambiguity; removing preconditions would weaken cooperating-writer guarantees. One snapshot preserves those guarantees and removes duplicate I/O. No architecture, path, setting, dependency or model call is added. Replaced private read/hash helpers have no remaining callers and are removed.

Independent primary references checked2026-10-02: [Python byte digests](https://docs.python.org/3/library/hashlib.html), [Git exact byte hashing](https://git-scm.com/docs/git-hash-object), [W3C exact text evidence](https://www.w3.org/TR/annotation-model/#text-quote-selector). These support exact identity; the size choice reuses the installed transaction contract rather than inventing a new bound.

Before repair, both regressions failed. After repair, the changed-file snapshot and the page above the former limit retain exact source bytes, while existing editorial-exemption checks pass. Installation and real CLI proof are recorded separately; no claim of complete audit closure is made.
