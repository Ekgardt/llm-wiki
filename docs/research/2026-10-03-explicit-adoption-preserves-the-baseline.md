# Explicit update adoption preserves the reviewed baseline

Research: 2026-10-02 through 2026-10-03. Python 3.12 runs this installation;
Python 3.10 remains supported. The existing install-manifest/v2 and
install-transaction/v2 schemas, paths and public --adopt flag remain unchanged.

The operator's explicit adoption of a changed shared resource already allowed
an update and kept the current bytes for immediate rollback. It did not update
the uninstall origin: uninstall could delete a file or the changed owned profile
block the operator had accepted. Three real-file regressions reproduced this:
two ordinary resources and one profile with foreign lines. Three other cases
passed against the original code, including default drift refusal and rejection
of a damaged historical preimage. An earlier interpretation that update adoption
itself was universally refused was incorrect; the initial four-case run had two
passes and two uninstall failures.

When explicit adoption is requested, the checkpoint now captures the actual
current projection once and records those verified bytes as both its baseline
and origin. Subsequent mutation still uses the existing compare-and-swap,
preimages, rollback, persistence and resource identity checks. Old referenced
preimages must validate before update; adoption does not excuse a damaged history.
Without the existing explicit adoption flag the strict path stays unchanged.
The initial candidate did not use the current snapshot consistently and still
failed two uninstall checks; the corrected candidate passed all six guards and
68 related tests. Further qualification is recorded separately.

The installed provider profile changed outside the earlier install checkpoint.
Its effective model selections must be preserved during the unrelated capture
budget repair. Reading current bytes is not enough to declare the old checkpoint
matching: that preliminary assertion correctly detected different bytes but was
an unnecessarily broad installation precondition. Any acceptance of the current
profile must use the canonical explicit takeover operation and preserve its
rollback evidence. This change does not select a new provider or model, change
permissions or hook trust, or approve unknown configuration. It makes the
existing explicit operation preserve the baseline through uninstall as promised.

Restoring old provider choices merely to get past a checkpoint would change
runtime behavior unrelated to capture. Writing hook files outside install control
would lose ownership and recovery evidence. Adding a second partial installer
would duplicate the control plane. Respecting the existing explicit takeover
contract is the narrower solution. Its tradeoff is that an explicitly accepted
owned projection becomes the uninstall baseline; default drift still requires
attention rather than being accepted automatically.

Sources checked during research:

- [IETF JSON Patch](https://www.rfc-editor.org/rfc/rfc6902), conditional update and
  failure semantics; no JSON Patch dependency is added.
- [Python dataclasses](https://docs.python.org/3.10/library/dataclasses.html),
  explicit immutable request replacement used by the existing resource API.
- [Linux rename(2)](https://man7.org/linux/man-pages/man2/rename.2.html), atomic
  file replacement and its limits; existing install persistence also fsyncs.
- The repository's canonical ManagedResource.adopt_current contract and
  docs/research/2026-09-28-a-rollback-undoes-only-what-it-did.md.

No live profile takeover or repaired queue is claimed by source tests alone.
