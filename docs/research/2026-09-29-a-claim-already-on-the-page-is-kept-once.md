# A claim already on the page is kept once

Date: 2026-09-29. Status: implemented in the same change.

## What happened (live vault)

The 03:00 nightly of 2026-09-29 ended `compile: FAILED — ValueError: compile claim
id already exists in target ledger` (journal of `llm-wiki-nightly.service`, 03:19:30
UTC), after the manual nightly of 2026-09-28 23:31 had compiled days that went on
growing. The whole compile failed, so no page of that run was written.

## Cause

`compile_memory._derived_claim` makes a claim's id from its date and the SHA-256
of its semantic payload (`claim-<date>-<fingerprint[:32]>`), and stores the full
`fingerprint`. A day compiled again — its receipt no longer matches once more
entries are appended — proposes the same claims again, under the same ids.
Inside one run a repeat was already dropped ("duplicate claim semantics");
`_merged_claims` refused the same repeat against the page's ledger and failed
the compile. Reproduced by `tests/test_a_claim_already_on_the_page_is_not_a_conflict.py`
on the old code.

## Rule

The id is content-addressed, so the same id with the same fingerprint is the same
claim, and adding it again stores nothing new — the property of any
content-addressed store: "you can insert any kind of content ... for which Git
will hand you back a unique key" (Pro Git, Git Objects,
https://git-scm.com/book/en/v2/Git-Internals-Git-Objects), and the same content
gets the same key. The page's copy is kept; an id whose stored fingerprint differs
from the new one is still refused, because there one key names two contents.

## Alternatives

- Skip a day that was compiled before: rejected, a grown day holds new entries
  whose claims are owed.
- Replace the page's claim with the new one: rejected, the page's claim carries
  the evidence the page was written from; the new one adds nothing to the ledger.
