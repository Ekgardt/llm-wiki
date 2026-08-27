# What a migration record binds

Date: 2026-08-26.
Question: `knowledge/notes/adoption-digest-is-provenance-decision.md` stopped
re-checking `sha256(scripts/integration_adapter.py)` on every memory write and
reduced the standing check to "the producer must still be installed". The
argument was that a digest says *different*, not *incompatible*, and that the
incompatibility that matters is already covered by schema digests. Before
leaving that in place: what do real migration records, lockfiles, and
provenance formats actually bind, and what do they check at write time versus at
migration time? And with the permanent check gone, does anything still stop a
downgrade?

## Finding 1 — the thing a migration record hashes is the migration, not the engine

Flyway is the strongest counter-example to the decision and it turns out not to
be one. Flyway computes a CRC32 of each migration **script** at apply time,
stores it in `flyway_schema_history`, and re-verifies it on *every* subsequent
startup; a changed script is a `Migration checksum mismatch` and the application
does not start. So a permanently re-checked digest is real, shipped practice.

But the subject of that hash is the file that *defined the change*. Nothing in
Flyway hashes `flyway-core.jar` and refuses to start when the engine is
upgraded — the engine is expected to change, and does, on every release. LLM
Wiki's frozen `installed_integration_sha256` is the engine, not the script:
`scripts/integration_adapter.py` is ordinary product code that other agents edit
and that the nightly fast-forward updates unattended. Re-checking it forever was
the equivalent of Flyway refusing to start after `brew upgrade flyway`.

Flyway also concedes the failure mode directly by shipping `flyway repair`,
whose entire job is to rewrite stored checksums when the permanent check has
become a dead end. The commonly cited hazard of `repair` — that it masks real
drift — is the same hazard the decision names when it rejects "re-record the
adoption after every update".

Alembic, by contrast, stores no script checksum at all. `alembic_version` holds
one column, `version_num`, the revision identifier. There is no integrity check
on migration bytes in the default schema; adding one is a custom extension.
Django's `django_migrations` and Rails' `schema_migrations` are the same shape:
app name, migration name, timestamp. Three of the four mainstream migration
frameworks bind an *identifier*, not a hash, and the fourth hashes the script
rather than the runtime.

The convention those three rely on instead is that **an applied migration is
immutable** — you never edit one, you add a new one. That is exactly the
contract the decision cites for refusing to re-record: the adoption record is
create-only evidence, so "re-record" means either violating immutability or
adding a second mutable record no reader can act on.

## Finding 2 — provenance is compared to an expectation, not re-derived

SLSA v1.0's verification procedure is explicit about where builder identity
sits. Step 1 is to "configure the verifier's roots of trust, meaning the
recognized builder identities", then "look up the SLSA Build Level in the roots
of trust, using the recognized public keys and the `builder.id`". Step 2 says
you SHOULD "compare the provenance against expected values", listing builder
identity and canonical source repository among them, where "expectations are
known provenance values that indicate the corresponding artifact is authentic".

Two things follow, and both match what the decision did:

1. The builder is an **identity checked against a preconfigured expectation**,
   not a digest of the builder's own bytes recomputed at read time. SLSA never
   asks a consumer to hash the build system.
2. The check happens **once, at artifact admission**, not on every later use of
   the artifact. Nothing in SLSA re-verifies provenance each time the installed
   binary is executed.

`_validate_migration_context` — which still refuses an adoption whose plan was
recorded against different adapter bytes — is the admission-time check.
`_require_adoption_sources` re-running it forever was an admission check
promoted to a runtime gate, which is the shape SLSA does not use.

## Finding 3 — forward compatibility is a declared format version, and this vault has none

The question the removed check was *not* answering is the one worth asking: what
stops older code, which predates V3, from opening the adopted databases?

The mainstream answer is a version field in the artifact that old readers
compare and refuse on. npm writes `lockfileVersion` into `package-lock.json`;
npm 6 meeting a v2 lockfile prints `npm WARN read-shrinkwrap This version of npm
is compatible with lockfileVersion@1, but package-lock.json was generated for
lockfileVersion@2`. SQLite offers `PRAGMA user_version` and `PRAGMA
application_id` for the same purpose, with the documented caveat that SQLite
itself does nothing with `user_version` — "it doesn't handle opening a database
with a newer user_version than your code knows about, but in that case you can
choose what to do." The guard is always the reader's, and it is always explicit.

LLM Wiki has no such guard, and the decision's claim that the digest "was never
what stood in [a downgrade's] way" is correct but leaves unstated what does.
Measured here on 2026-08-26: an adopted `run/queue.sqlite3` is a JSON tombstone,
and a pre-V3 reader opening it through `sqlite3` fails —

```
>>> c = sqlite3.connect(tombstone); c.execute("CREATE TABLE IF NOT EXISTS t(x)")
sqlite3.DatabaseError: file is not a database
```

— but only on the first statement that touches a page. `sqlite3.connect()`
succeeds, and `SELECT 1` returns `(1,)`, because neither reads the file header.
`scripts/memory_queue.py` never unlinks or recreates `queue.sqlite3`, so nothing
clobbers the tombstone.

So the downgrade does fail closed, and the decision is right that removing the
producer digest did not weaken it. But the barrier is a **byproduct of the
replacement file not being a database**, not a designed refusal: the error names
neither the adoption nor the version, and a reader that only ever runs
statements not touching a page would not notice at all. Every comparable system
puts a declared version in the artifact and has the reader refuse on it by name.

## Does the decision hold?

Yes, on its own terms, and the sources strengthen it rather than qualify it:

- Re-hashing the producer on every write has no analogue in migration
  frameworks, lockfiles, or provenance formats. Flyway's permanent check is on
  the migration script; nobody re-checks the engine.
- Treating the frozen digest as provenance bound at admission time is what SLSA
  prescribes for builder identity.
- Rejecting "re-record after every update" is the migration-immutability
  convention, and Flyway's `repair` is the cautionary example of what the
  alternative becomes.
- Leaving schema digests fail-closed is the right residual: shape mismatch is
  the incompatibility that matters, and it is the one thing the digest could
  never express.

## What this research does not claim

- It does not verify that `queue_schema_sha256`,
  `coordinator_schema_sha256` and `adoption_schema_sha256` cover every shape
  change that could matter. That is a claim about this codebase's schema
  coverage and was not measured.
- It does not claim the presence check is sufficient. The decision's own open
  question — a `scripts/` replaced wholesale by a different product passes
  presence and fails later on schema — stands unaddressed by anything found
  here.
- It does not establish a downgrade *policy*. It establishes only that on this
  runtime, today, a pre-V3 reader hits `file is not a database` on first page
  access, and that the field's answer to this problem is a declared format
  version that this vault does not have.
- No source was found that addresses the exact case of a record freezing the
  digest of the *code that performed the migration*, which suggests the practice
  is uncommon rather than that it is settled against.

## Sources

- Flyway, `flyway_schema_history` checksums, validate-on-startup, and `repair` —
  flyway/flyway issue #2255 ("Validate failed: Migration checksum mismatch"),
  issue #2474, and Baeldung's write-up of `flyway repair` with Spring Boot.
  https://github.com/flyway/flyway/issues/2255 ·
  https://www.baeldung.com/spring-boot-flyway-repair
- Alembic, `alembic_version` holding only `version_num` —
  sqlalchemy/alembic discussion #1075, "Storing extra information in
  `alembic_version` table".
  https://github.com/sqlalchemy/alembic/discussions/1075
- Django, `django_migrations` recording app, migration name and timestamp, and
  the immutable-migration convention.
  https://docs.djangoproject.com/en/6.0/topics/migrations/
- SLSA v1.0, "Verifying artifacts" — roots of trust, `builder.id` lookup, and
  "expectations are known provenance values".
  https://slsa.dev/spec/v1.0/verifying-artifacts ·
  https://slsa.dev/spec/v1.0/provenance
- npm, `lockfileVersion` and the `read-shrinkwrap` warning old npm emits on a
  newer lockfile.
  https://docs.npmjs.com/cli/v8/configuring-npm/package-lock-json/
- SQLite, `PRAGMA user_version` / `application_id` as an application-owned
  schema version the reader must act on itself.
  https://gluer.org/blog/sqlites-user_version-pragma-for-schema-versioning/
- Measured here, 2026-08-26: `sqlite3` behaviour against an
  `operational-db-tombstone/v1` file; `scripts/memory_queue.py` path handling.
