# Both daily writers reach compilation and historical evidence

Research and diagnosis: 2026-09-29; baseline a5c67aee, Python 3.14.6.

The installed compiler rejects an 80,926-byte marker-only part against a
27,744-token input allowance. This is not one oversized entry: its 80 daily
blocks are at most 2,529 bytes. The compiler's marker-only partition ignores
the heading writer. All 26 distinct failing historical day/digest pairs were
recovered byte-for-byte by searching both entry starts. No digest was rewritten.

The data path is writer → daily → snapshot/parts → budget packing → atomic
receipt/page commit → resume/diagnostic mirror → archive → evidence reader.
Codebase Memory traced the partition's ten callers; source inspection confirmed
archive receipt selection and doctor's supersession proof also need the rule.

## Sources and alternatives

Primary sources checked on the research date:

- [Python hashlib](https://docs.python.org/3/library/hashlib.html): incremental
  hashing preserves the digest of concatenated bytes. Historical reads must
  find exact original bytes, never substitute a current hash.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): a committed
  transaction is the authority for success; preserve the existing receipt
  validation rather than inferring success from source contents.
- [BagIt RFC 8493](https://www.rfc-editor.org/rfc/rfc8493.html): payload integrity
  is checked against recorded manifests. Existing sealed part tables retain
  their meaning independently of the new splitting rule.
- [RFC 9162](https://www.rfc-editor.org/rfc/rfc9162.html): append-only consistency
  motivates verifying retained bytes, not replacing failed historical evidence.
  This application does not implement a Certificate Transparency log.

Chosen: preserve marker-sized partitions and already committed oversized
part identities; subdivide only oversized uncommitted parts using both entry
formats. Every consumer uses the same partition helper and authoritative
receipt predicate. Search historical evidence from both kinds of start.
This requires no new dependency, runtime location, or receipt format.

Rejected: increasing the model window (does not repair the writer/reader
contract), rewriting evidence hashes (destroys the integrity proof), and
globally repartitioning all days (repeats previously accepted work).

Tradeoff: the marker partition remains necessary compatibility logic for
existing receipt identities. Remove it only after all retained flat days and
in-flight snapshots have migrated to an independently recorded part table.
Preserved boundaries can leave short adjacent parts. With N=day_bytes/part_bytes,
the original partition has <=2N+1 parts and subdivision adds <2N; the archive
schema therefore admits 4N+1 rather than rejecting valid producer output.
Individual entries larger than the actual model budget still fail explicitly;
this change does not truncate them or claim to solve every possible large entry.

Regression coverage includes heading-only real-scale input, byte coverage,
packing, resumed work, old committed parts, old/new archive round trips,
historical digests after append, and rejection of changed source bytes.
