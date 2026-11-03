# Release manifest

This owner retains the complete existing `scripts/release_manifest.py` and
`tests/test_release_manifest.py`, with their paths and bytes unchanged. It reads
Git commit identities and committed blobs, calculates SHA-256, and renders plain
or Markdown release notes. It does not create or push tags. `release_tag` is a
separate consumer; its full source is not a reverse dependency of this helper.

## Preserved behavior

`commit_of(ref)` runs `git rev-parse <ref>^{commit}` in the module's checkout root
and returns stripped stdout. Unknown refs exit with the existing message.
`manifest(ref, root=ROOT)` hashes the exact `git show <ref>:<path>` bytes for the
six `PINNED_FILES`, preserving their order. Missing blobs exit. The default root
is bound when the function is defined; the explicit root belongs to the caller.
The existing 60-second Git timeout and subprocess errors remain unchanged.

`markdown(ref, oid, hashes)` renders the commit, exact bootstrap assignment and
digest table. The CLI accepts the existing ref and `--markdown` flag; it resolves
the commit, reads the manifest and prints the selected form. Rendering is not an
authentication or signature check. No stronger Git-ref validation is claimed by
this decomposition.

The complete checkout/ref supplied to Git is external data. Install scripts and
the lockfile are read as opaque blob bytes, never imported or executed by this
helper. Their algorithm-independent contents are not maintenance dependencies
of SHA-256 hashing. The original tests still read real HEAD blobs; they are not
replaced by a mock or a truncated fixture. The release-tag consumer and installer
implementations need their own complete maintenance packets.

## Boundary and validation

The implementation imports only the standard library. Static and isolated import
tests enforce that property; ordinary imports do not run the CLI. The existing
three tests check a full OID, all pinned digests and the rendered command. Run
them with `tests/modules/release_manifest`, then the context/ownership gates.
The manifest includes all source, tests, instructions and tooling necessary for
this owner and retains the unchanged 100000 o200k_base ceiling.

## Research and decision — 2026-11-03

The approved whole-project plan permits independent responsibility boundaries.
Keep the existing concrete helper: a new runtime interface, distribution or
file move would add complexity without improving this already independent
boundary. Combining it with tag orchestration and that consumer's full
CHANGELOG input measured 114513 tokens before a new guide, so that combination
is not qualified. Historical-record treatment remains separate work; the
failure is not an exception to the ceiling.

Sources checked on this date: [Fowler's bounded context](https://martinfowler.com/bliki/BoundedContext.html)
supports explicit responsibility relationships;
[Import Linter contracts](https://import-linter.readthedocs.io/en/stable/contract_types/)
support executable dependency constraints;
[PEP 544](https://peps.python.org/pep-0544/) describes optional structural typing,
not a need to introduce a new interface here. Concrete existing Git behavior was
checked against [rev-parse](https://git-scm.com/docs/git-rev-parse) and
[tag](https://git-scm.com/docs/git-tag). No dependency or supported version changes.

Evidence: built-in checkout graph generation
`generation-18e49761e7127892-6a1d4f1f`, supplemented by actual source, callers,
tests and `.github/workflows/release-tag.yml`; original release tests: 8 passed.
This qualification itself performs no release, tag publication or installation.
