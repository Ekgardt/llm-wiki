# Open documents are a cache, not a ledger

Dated 2026-09-17. Finding K-A12 of the third audit. The research before the fix. The owner
delegated the decision.

Files: scripts/pyright_session.py, tests/fake_lsp_server.py,
tests/test_the_open_documents_make_room_for_the_next_file.py,
tests/test_pyright_session.py

## What was found

`LanguageServerSession` holds at most 256 open documents and 64 MiB of their source. A
document left that set only when the workspace deleted or renamed it, or when the session
closed. There was no eviction and no `textDocument/didClose`. So:

- the 257th distinct file in one checkout raised `RuntimeError`, and
  `code_navigation._open_document` turns that into a structural fallback -- for that file and
  every file after it, for the life of the MCP process;
- `synchronize` runs before every precise query, and it re-read and re-hashed **every** open
  document each time, to confirm the bytes the server holds against the workspace revision.
  With 200 documents open that is 200 reads and 200 SHA-256 per query.

## Practice on this date

- The protocol says the client owns the document's lifetime, and that closing is how it gives
  ownership back: "The document close notification is sent from the client to the server when
  the document got closed client-side. The document's master now exists where the document's
  Uri points to (e.g. if the document's Uri is a file Uri the master now exists on disk)."
  (LSP 3.17, DidCloseTextDocument Notification,
  <https://github.com/microsoft/language-server-protocol/blob/gh-pages/_includes/messages/3.17/textDocument_didClose.md>,
  fetched today.) A server keeps an open document in memory until it is told otherwise, so a
  client that never closes is the one leaking.
- Nothing in the protocol requires a client to keep a document open once it is done with it,
  and every real editor closes documents as tabs close. The open set is a cache of what the
  server has been told, not a record of what the repository contains -- the workspace
  revision is that record.

## The decision

1. **The open set evicts.** When one more document would break the count or the byte bound,
   the session closes the ones it has gone longest without using -- `textDocument/didClose`
   first, then it forgets the document, its readiness, its diagnostics and its wire
   bookkeeping. Use order is a counter per URI, not the order of `self._documents`, because
   replay after a restart must keep walking documents in the order they were opened.
   A document with a `didOpen` in flight is never evicted. A document larger than the whole
   byte budget evicts nothing -- no amount of closing would let it in -- and still raises, so
   the "refuses without partial session state" contract holds for that case.
2. **`synchronize` confirms a retained document only when its file has moved.** The session
   remembers `(st_dev, st_ino, st_size, st_mtime_ns)` for each document at the moment its
   bytes were last confirmed. If the file still matches, the confirming read is skipped.
   **What that trades:** a rewrite that keeps the same device, inode, length and nanosecond
   modification time is no longer caught by the confirming read. It is still caught by the
   workspace revision, which is computed from disk by the caller before every synchronize and
   is what decides which documents changed; this only decides whether the second, confirming
   read is needed. The identity is recorded only when a stat before the read and a stat after
   it agree, so a file changing during the read is not cached.
3. **The fake server learned `textDocument/didClose`.** It kept a closed document registered
   and then refused the reopen as a duplicate `didOpen`, which is not how a real server
   behaves. One handler, additive.

## What this costs

One `os.stat` per open document per synchronize instead of one read plus one SHA-256; one
`didClose` notification per evicted document, on the caller that needed the room.
