# Host transcript roots

`host_transcripts.host_transcript_roots` computes the ordered, deduplicated roots
for Claude and Codex transcripts. Defaults remain present when configured roots
are added; blank values are ignored and configured tildes are expanded. Explicit
environment/home arguments support callers without changing global state.
This module does not read transcripts or authorize paths: adapter/flush consumers
retain containment, symlink and extension checks. Returning a root is not proof
that a transcript is safe to read.

Keep scripts/host_transcripts.py byte-identical and retain the existing host
environment variable names and order. Unit qualification covers defaults,
configuration, whitespace, deduplication and explicit arguments; the moved-host
adapter tests remain real consumer obligations. Historical design:
../research/2026-09-17-a-moved-host-directory-is-still-the-host.md.
Run `pytest tests/modules/host_transcripts
 tests/test_a_moved_host_directory_is_still_the_host.py -q` as one command.

Shared canonical contract: [Maintenance qualification](context-tooling.md).

## Structural reference retained from the overview

### Existing leaf owners

The existing `okf_types`, `host_transcripts`, `ephemeral_paths`,
`embedding_model` and `inference_threads` files are qualified in place,
with distinct complete packets and retained consumer tests. Their production
paths, bytes, host configuration, model pins and process-local state ownership
stay unchanged. See
[research](../research/2026-11-03-existing-leaf-owner-qualification.md).

Full contracts: [okf types](okf-types.md), [host transcripts](host-transcripts.md), [ephemeral paths](ephemeral-paths.md), [embedding model](embedding-model.md), [inference threads](inference-threads.md).
