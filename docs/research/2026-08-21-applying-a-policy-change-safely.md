# Applying a change to a live enforcement policy — 2026-08-21

Why this was needed: rule 2 refused a new module,
`docs/enforcement/apply-rules-1-2-cover-bash.py`, which patches the root-owned
gate that decides whether other work is allowed. Changing the thing that
authorises changes is exactly the case where getting it wrong is unrecoverable,
so the question is what current practice does about it.

## What was asked

How does current practice apply a change to a policy that is enforcing, when a
bad change can stop the system answering at all?

## What the sources say

Four points recur, and they agree with each other.

**Validate before it takes effect.** Generated configuration is checked for
structure, conflicts and safety invariants before deployment rather than after.
For this case that means: the patched Python must parse and the patched JSON
must load, checked before anything relies on them.

**Do not go straight to enforcing.** The staged pattern is dry-run, then
shadow, then canary, then full enforce. There is one gate here and no traffic
to split, so the usable part is the first stage: prove the gate still returns a
verdict before trusting it to guard anything.

**Rollback is instantaneous and automatic, not a procedure someone follows.**
The control state returns to the last known good policy without waiting for a
human to notice. The failure this prevents is precisely the one recorded in the
gate's own docstring: a self-recursive write once made every call raise, the
gate blocked all thirteen cases at once, and nobody could repair it.

**Keep a break-glass path.** Conditional-access guidance puts a break-glass
account first, before any policy is switched on, so a policy that locks
everyone out is survivable.

## What this means for the script

- Copy both files aside first, into the backups directory that already exists.
- Refuse to start unless the anchor appears exactly once and the three policy
  entries are all present — a partial match means the file is not what the
  patch was written against.
- Write, then verify: `ast.parse` the Python, `json.loads` the JSON, and run
  the gate on a trivial payload. Exit 0 or 2 are both verdicts; anything else
  is the gate failing to answer.
- Restore both files on any failure, including an exception mid-write, and exit
  non-zero. The backups are the break-glass path, and their location is printed
  on success so it is known before it is needed.

Not adopted: canary and shadow stages. There is a single gate on a single
machine, so there is no population to split and no shadow traffic to compare
against. Saying so is better than pretending a four-stage rollout happened.

## Sources

- https://cloudmatos.ai/blog/opa-canary-deployments-smart-policy-versioning/
- https://www.decryptiondigest.com/blog/entra-id-conditional-access-deployment-guide
- https://sreschool.com/blog/rollback/
- https://arxiv.org/pdf/2512.23774
