# LSP byte codec

`llm_wiki.lsp_wire` owns strict JSON validation, Content-Length framing, canonical
UTF-8 encoding and bounded consumption of oversized frames. A binary stream is
supplied by the caller and remains caller-owned. The codec neither closes streams
nor starts threads, processes, timers or transport state.

Its only product dependency is `bounded_io.IO_CHUNK_BYTES`; the packet includes
that full source and its path_ancestry dependency. All pre-existing limits,
rejection messages, integer rules, surrogate handling and duplicate-key refusal
are unchanged. Oversized frames are consumed before FrameTooLarge is raised;
recoverable response identity remains subject to the existing edge-only rules.

`lsp_protocol` re-exports the existing public codec names and constants for
lsp_process, pyright_session, fake peers and integration tests. The transport
continues to call its encode_frame binding, preserving the intentional fault
injection in process cancellation tests. These aliases have no second algorithm.
They can be removed only after every supported import and serialization consumer
migrates; public __module__ names remain lsp_protocol for compatibility.

Eighteen pure test functions and their two helpers moved without changing bodies
or parameters. They no longer repeat under an unrelated close-policy fixture;
all transport tests retain both close paths. Mixed oversized-response lifecycle
and process tests remain integration obligations outside this owner. The isolated
import guard denies transport, profile and process modules. Run
`pytest tests/modules/lsp_wire tests/test_lsp_protocol.py
 tests/test_an_oversized_reply_fails_its_request_not_its_server.py -q`.

This packet does not introduce a newer protocol, a dependency, a minimum-Python
change, or a native-platform qualification claim. See the dated boundary research
for alternatives, primary sources and measured evidence.

## Structural reference retained from the overview

### LSP wire codec

`llm_wiki.lsp_wire` owns strict JSON and byte framing, consuming a caller-owned
binary stream. `lsp_protocol` retains transport threads, cancellation and stream
lifecycle. Existing imports remain aliases; wire limits and rejection behavior
are preserved. See [boundary research](../research/2026-11-03-lsp-wire-boundary.md).

Full owner contract: [LSP wire](lsp-wire.md).
