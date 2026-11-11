"""Open documents as a bounded cache, and the plan that synchronizes them with a workspace revision."""

import hashlib
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bounded_io import read_stable_bytes
from llm_wiki.pyright_session_values import OpenDocument, validated_deadline
from lsp_process import LspProcess
from lsp_protocol import (
    MAX_FRAME_BYTES,
    JsonRpcResponseError,
    ProtocolViolation,
    encode_frame,
)
from lsp_security import RepositorySource, resolve_repository_source
from workspace_revision import (
    WorkspaceDelta,
    WorkspaceRevision,
    diff_workspace_revisions,
)

MAX_DOCUMENT_BYTES = MAX_FRAME_BYTES - 1
# Session memory for open documents, least recently used closed first: 256 documents or 64 MiB.
# Value predates measurement; review if a large repository keeps evicting.
MAX_OPEN_DOCUMENTS = 256
MAX_OPEN_DOCUMENT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SyncPlan:
    """Everything one synchronize pass settled before touching the server."""

    process: LspProcess
    generation: str
    prior: WorkspaceRevision | None
    revision: WorkspaceRevision
    deadline: float
    documents_snapshot: dict[str, OpenDocument]
    next_documents: dict[str, OpenDocument]
    projected_document_bytes: int
    closed_documents: list[OpenDocument]
    changed_replacements: list[tuple[OpenDocument, OpenDocument, dict[str, object]]]
    close_notifications: list[tuple[OpenDocument, dict[str, object]]]
    watched_params: dict[str, object] | None


def without_wire_uri(
    keys: set[tuple[object, ...]], generation: str, uri: str
) -> set[tuple[object, ...]]:
    """The wire keys with every entry for this generation's URI removed."""
    return {key for key in keys if not (key[0] == generation and key[1] == uri)}


@dataclass(frozen=True)
class _SyncSnapshot:
    """The session state one synchronize pass plans against."""

    process: LspProcess
    generation: str
    prior: WorkspaceRevision | None
    documents_snapshot: dict[str, OpenDocument]
    open_by_path: dict[str, OpenDocument]


def _first_sync_delta(
    open_by_path: Mapping[str, OpenDocument],
    entries: Mapping[str, object],
) -> WorkspaceDelta:
    """Without a prior revision, open documents are compared to this one."""
    changed = tuple(
        sorted(
            path
            for path, document in open_by_path.items()
            if _entry_supersedes(entries.get(path), document)
        )
    )
    deleted = tuple(
        sorted(path for path in open_by_path if _entry_missing(entries.get(path)))
    )
    return WorkspaceDelta((), changed, (), deleted)


def _entry_missing(entry: object) -> bool:
    """The revision does not describe a usable file at this path."""
    return entry is None or getattr(entry, "sha256", None) is None


def _entry_supersedes(entry: object, document: OpenDocument) -> bool:
    """The revision describes different bytes than the open document holds."""
    if _entry_missing(entry):
        return False
    return entry.sha256 != document.source_sha256


def _validated_entry_size(entry: object) -> int:
    """The entry's byte count, refused when it is not a sane size."""
    size = entry.size
    if isinstance(size, bool) or not isinstance(size, int):
        raise RuntimeError("Pyright source document byte limit exceeded")
    if size < 0 or size > MAX_DOCUMENT_BYTES:
        raise RuntimeError("Pyright source document byte limit exceeded")
    return size


def _documents_fit(count: int, used: int) -> bool:
    """Whether this many open documents of this size are within both bounds."""
    if count > MAX_OPEN_DOCUMENTS:
        return False
    return used <= MAX_OPEN_DOCUMENT_BYTES


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    """What a file would have to keep for its bytes to still be the same bytes.

    Device and inode say it is the same file; size and nanosecond modification
    time say it was not rewritten. A rewrite that keeps all four -- the same
    length written inside one clock tick -- is the one change this misses, and
    it is what `synchronize` trades for not re-reading every open document on
    every query. The workspace revision is still authoritative about what
    changed: this only decides whether the second, confirming read is needed.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _retained_document_bytes(entry: object, document: OpenDocument) -> int:
    """The revision's size for a document that must stay open."""
    if _entry_missing(entry):
        raise RuntimeError(
            "Pyright open document is absent from the workspace revision"
        )
    size = _validated_entry_size(entry)
    if entry.sha256 == document.source_sha256 and size != len(document.content):
        raise RuntimeError("Pyright source document size differs from the revision")
    return size


def verified_document_content(
    entry: object, document: OpenDocument, *, deadline: float
) -> bytes:
    """The document re-read from disk, checked against the revision's entry."""
    content = read_stable_bytes(
        document.source.absolute_path,
        MAX_DOCUMENT_BYTES,
        label="Pyright retained source document",
        deadline=deadline,
    )
    digest = hashlib.sha256(content).hexdigest()
    if entry.sha256 != digest or entry.size != len(content):
        raise RuntimeError(
            "Pyright retained source document hash differs from the revision"
        )
    return content


def _check_changed_entry(entry: object, digest: str, content: bytes) -> None:
    """A changed document has to match what the revision says it became."""
    if entry is None or entry.sha256 != digest or entry.size != len(content):
        raise RuntimeError(
            "Pyright changed source document hash differs from the revision"
        )


def _check_encodable(method: str, params: dict[str, object]) -> None:
    """Refuse now what the wire would refuse later."""
    encode_frame({"jsonrpc": "2.0", "method": method, "params": params})


def _collect_closed(
    document: OpenDocument | None,
    closed: list[OpenDocument],
    closed_uris: set[str],
) -> None:
    """Record a document to close, once per URI."""
    if document is None or document.source.uri in closed_uris:
        return
    closed.append(document)
    closed_uris.add(document.source.uri)


def _close_notifications(
    closed: list[OpenDocument],
) -> list[tuple[OpenDocument, dict[str, object]]]:
    """The didClose params for every document being closed."""
    notifications = [
        (document, {"textDocument": {"uri": document.source.uri}})
        for document in closed
    ]
    for _document, params in notifications:
        _check_encodable("textDocument/didClose", params)
    return notifications


def _watched_params(watched: list[dict[str, object]]) -> dict[str, object] | None:
    """The watched-files params, or None when nothing was watched."""
    if not watched:
        return None
    params: dict[str, object] = {"changes": list(watched)}
    _check_encodable("workspace/didChangeWatchedFiles", params)
    return params


def _next_documents(
    documents_snapshot: dict[str, OpenDocument],
    closed: list[OpenDocument],
    replacements: list[tuple[OpenDocument, OpenDocument, dict[str, object]]],
) -> dict[str, OpenDocument]:
    """The open-document map after the planned closes and replacements."""
    next_documents = dict(documents_snapshot)
    for document in closed:
        next_documents.pop(document.source.uri, None)
    for document, replacement, _params in replacements:
        next_documents[document.source.uri] = replacement
    return next_documents


def _check_document_unchanged(
    current: OpenDocument,
    source: RepositorySource,
    content: bytes,
    digest: str,
) -> None:
    """An open document must not change behind the session's back."""
    if (
        current.source != source
        or current.content != content
        or current.source_sha256 != digest
    ):
        raise RuntimeError(
            "open Pyright document changed without synchronization"
        )


def _check_did_open_encodable(params: dict[str, object]) -> None:
    """Refuse a document the wire could never carry."""
    try:
        encode_frame(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": params,
            }
        )
    except ProtocolViolation as error:
        raise ValueError(
            "Pyright source document exceeds the LSP frame"
        ) from error


class SessionDocuments:
    """Open, evict and plan the synchronization of the session's documents."""

    def _probe_document(
        self,
        document: OpenDocument,
        process: LspProcess,
        *,
        deadline: float,
    ) -> None:
        with self._lock:
            supported = self._capabilities.get("document_symbols", False)
        if not supported:
            self._mark_protocol_initialized(did_open=True, deadline=deadline)
            return
        try:
            result = process.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": document.source.uri}},
                deadline=deadline,
            )
        except (JsonRpcResponseError, ProtocolViolation, RuntimeError, TimeoutError):
            with self._lock:
                self._ready_uri_generations.pop(document.source.uri, None)
                self._refresh_readiness_locked(deadline=deadline)
            return
        if self._normalize_document_symbols(result, document.source.uri)[1]:
            with self._lock:
                self._ready_uri_generations.pop(document.source.uri, None)
                self._refresh_readiness_locked(deadline=deadline)
            return
        with self._lock:
            generation = process.generation_nonce
            self._generation_nonce = generation
            self._ready_uri_generations[document.source.uri] = generation
            self._refresh_readiness_locked(deadline=deadline)

    def _document_source_bytes(
        self, path: str, deadline: float
    ) -> tuple[RepositorySource, bytes, str]:
        """The resolved source, its verified bytes, and their digest."""
        source = resolve_repository_source(self._repository, path)
        content = read_stable_bytes(
            source.absolute_path,
            MAX_DOCUMENT_BYTES,
            label="Pyright source document",
            deadline=deadline,
        )
        content.decode("utf-8", errors="strict")
        return source, content, hashlib.sha256(content).hexdigest()

    def _try_process_did_open(
        self, document: OpenDocument, process: LspProcess, deadline: float
    ) -> bool:
        """Announce the document; a refusal is an answer, not a failure."""
        try:
            return self._send_process_did_open(document, process, deadline=deadline)
        except (ProtocolViolation, RuntimeError, TimeoutError):
            return False

    def _reopen_existing_document(
        self,
        current: OpenDocument,
        process: LspProcess,
        *,
        ready: bool,
        deadline: float,
    ) -> OpenDocument:
        """A document we already hold; re-announce it if the server lost it."""
        if ready:
            return current
        did_open = self._try_process_did_open(current, process, deadline)
        self._mark_protocol_initialized(did_open=did_open, deadline=deadline)
        if not did_open:
            return current
        self._probe_document(current, process, deadline=deadline)
        return current

    def _register_document_locked(
        self, document: OpenDocument, content: bytes
    ) -> None:
        """Take the document into the open set, within its count and byte bounds."""
        if len(self._documents) >= MAX_OPEN_DOCUMENTS:
            raise RuntimeError("Pyright open document count limit exceeded")
        document_bytes = self._document_bytes + len(content)
        if document_bytes > MAX_OPEN_DOCUMENT_BYTES:
            raise RuntimeError("Pyright open document source bytes limit exceeded")
        uri = document.source.uri
        self._documents[uri] = document
        self._document_bytes = document_bytes
        self._readiness_target_uri = uri
        self._ready_uri_generations.pop(uri, None)
        self._note_document_use_locked(uri)

    def _evictable_document_locked(self, document: OpenDocument) -> bool:
        """Whether closing this document would cut across work in flight."""
        uri = document.source.uri
        return not any(key[1] == uri for key in self._wire_sending)

    def _documents_by_use_locked(self) -> list[OpenDocument]:
        """The open documents, the one untouched longest first."""
        return sorted(
            self._documents.values(),
            key=lambda document: self._document_used.get(document.source.uri, 0),
        )

    def _crowded_documents_locked(self, incoming_bytes: int) -> list[OpenDocument]:
        """The least recently used documents to close so one more fits.

        An empty list means one more already fits within both bounds -- or, for
        a document larger than the whole budget, that no amount of closing
        would help, so nothing is given up to make room it could not use.
        """
        if incoming_bytes > MAX_OPEN_DOCUMENT_BYTES:
            return []
        return self._evictions_to_fit_locked(
            len(self._documents) + 1, self._document_bytes + incoming_bytes
        )

    def _evictions_to_fit_locked(self, count: int, used: int) -> list[OpenDocument]:
        """Least recently used evictable documents, until `count` and `used` fit."""
        crowded: list[OpenDocument] = []
        for document in self._documents_by_use_locked():
            if _documents_fit(count, used):
                break
            if self._evictable_document_locked(document):
                crowded.append(document)
                count -= 1
                used -= len(document.content)
        return crowded

    def _forget_wire_uri_locked(self, uri: str) -> None:
        """Forget that this URI was announced; a reopen has to announce it again."""
        generation = self._wire_generation
        if generation is None:
            return
        self._wire_opened = without_wire_uri(self._wire_opened, generation, uri)
        self._wire_failed = without_wire_uri(self._wire_failed, generation, uri)

    def _forget_document_locked(self, document: OpenDocument) -> None:
        """Drop a closed document and everything else keyed to its URI."""
        uri = document.source.uri
        if self._documents.get(uri) is not document:
            return
        del self._documents[uri]
        self._document_bytes -= len(document.content)
        self._document_identities.pop(uri, None)
        self._document_used.pop(uri, None)
        self._ready_uri_generations.pop(uri, None)
        self._forget_wire_uri_locked(uri)
        snapshot = self._diagnostics.pop(uri, None)
        self._diagnostic_bytes -= 0 if snapshot is None else snapshot.retained_bytes
        self._forget_readiness_target_locked(uri)

    def _forget_readiness_target_locked(self, uri: str) -> None:
        """A session whose proven document is gone is only initialized again."""
        if self._readiness_target_uri != uri:
            return
        self._readiness_target_uri = None
        self._relax_readiness_locked(self._documents, None)

    def _close_evicted_document(
        self, document: OpenDocument, process: LspProcess, deadline: float
    ) -> None:
        """Tell the server we are done with a document, then forget it.

        The notification is best effort, as `didOpen` is: a server that refused
        it, or a generation that has since been replaced, will not be holding
        the document either way.
        """
        params = {"textDocument": {"uri": document.source.uri}}
        generation = self._current_generation_nonce()
        if generation is not None:
            self._try_process_did_close(process, params, generation, deadline)
        with self._lock:
            self._forget_document_locked(document)

    @staticmethod
    def _try_process_did_close(
        process: LspProcess,
        params: dict[str, object],
        generation_nonce: str,
        deadline: float,
    ) -> None:
        try:
            process.notify_generation(
                "textDocument/didClose",
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )
        except (ProtocolViolation, RuntimeError, TimeoutError):
            return

    def _make_room_for_document(
        self, incoming_bytes: int, process: LspProcess, deadline: float
    ) -> None:
        """Close the least recently used documents until one more would fit."""
        with self._lock:
            crowded = self._crowded_documents_locked(incoming_bytes)
        for document in crowded:
            self._close_evicted_document(document, process, deadline)

    def _open_new_document(
        self,
        source: RepositorySource,
        content: bytes,
        digest: str,
        process: LspProcess,
        deadline: float,
    ) -> OpenDocument:
        document = OpenDocument(source, content, digest, 1)
        _check_did_open_encodable(self._did_open_params(document))
        self._make_room_for_document(len(content), process, deadline)
        with self._lock:
            self._register_document_locked(document, content)
        sent = self._try_process_did_open(document, process, deadline)
        self._mark_protocol_initialized(did_open=sent, deadline=deadline)
        if not sent:
            return document
        self._probe_document(document, process, deadline=deadline)
        return document

    def _reused_document_locked(self, uri: str) -> OpenDocument | None:
        """The document already held for this URI, marked as used just now."""
        document = self._documents.get(uri)
        if document is None:
            return None
        self._note_document_use_locked(uri)
        return document

    def _note_document_use_locked(self, uri: str) -> None:
        """Order documents by use without reordering `self._documents` itself.

        Replay after a restart walks `self._documents`, and that has to stay the
        order the documents were opened in; only eviction cares about use.
        """
        self._document_use += 1
        self._document_used[uri] = self._document_use

    def _open_document_within_operation(
        self, path: str, deadline: float
    ) -> OpenDocument:
        self.start(deadline=deadline)
        source, content, digest = self._document_source_bytes(path, deadline)
        with self._lock:
            current = self._reused_document_locked(source.uri)
            process = self._process
            ready = self._document_ready_locked(source.uri)
        if process is None:
            raise RuntimeError("Pyright session is not protocol initialized")
        if current is None:
            return self._open_new_document(source, content, digest, process, deadline)
        _check_document_unchanged(current, source, content, digest)
        return self._reopen_existing_document(
            current, process, ready=ready, deadline=deadline
        )

    def open_document(self, path: str, *, deadline: float) -> OpenDocument:
        deadline = validated_deadline(deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright document deadline expired")
        with self._document_operation_lock(deadline), self._operation():
            return self._open_document_within_operation(path, deadline)

    def _synchronize_snapshot(self) -> _SyncSnapshot:
        """The process, generation and open documents this pass plans against."""
        with self._lock:
            process = self._process
            prior = self._workspace_revision
            documents_snapshot = dict(self._documents)
            generation = self._generation_nonce
        if process is None or generation is None:
            raise RuntimeError("Pyright session is not protocol initialized")
        open_by_path = {
            document.source.relative_path: document
            for document in documents_snapshot.values()
        }
        return _SyncSnapshot(
            process, generation, prior, documents_snapshot, open_by_path
        )

    def _synchronize_delta(
        self,
        snapshot: _SyncSnapshot,
        revision: WorkspaceRevision,
        entries: Mapping[str, object],
    ) -> WorkspaceDelta:
        if snapshot.prior is None:
            return _first_sync_delta(snapshot.open_by_path, entries)
        return diff_workspace_revisions(snapshot.prior, revision)

    def _projected_document_bytes(
        self,
        snapshot: _SyncSnapshot,
        entries: Mapping[str, object],
        closing_paths: set[str],
    ) -> int:
        """Bytes the retained documents will occupy, refused when they overrun."""
        total = 0
        for path, document in snapshot.open_by_path.items():
            if path in closing_paths:
                continue
            total += _retained_document_bytes(entries.get(path), document)
            if total > MAX_OPEN_DOCUMENT_BYTES:
                raise RuntimeError("Pyright open document source bytes limit exceeded")
        return total

    def _verified_retained_contents(
        self,
        snapshot: _SyncSnapshot,
        entries: Mapping[str, object],
        closing_paths: set[str],
        *,
        deadline: float,
    ) -> dict[str, bytes]:
        """Check every retained document against the revision, re-reading it
        only when its file has moved since the last check."""
        contents: dict[str, bytes] = {}
        for path, document in snapshot.open_by_path.items():
            if path in closing_paths:
                continue
            content = self._retained_content(
                document, entries[path], deadline=deadline
            )
            if content is not None:
                contents[path] = content
        return contents

    def _retained_content(
        self, document: OpenDocument, entry: object, *, deadline: float
    ) -> bytes | None:
        """The document re-read and checked, or None when its file has not moved.

        Without this, every query paid one read and one SHA-256 for every open
        document, because `synchronize` runs before each one.
        """
        path = document.source.absolute_path
        before = _file_identity(path)
        with self._lock:
            known = self._document_identities.get(document.source.uri)
        if before is not None and before == known:
            return None
        content = verified_document_content(entry, document, deadline=deadline)
        self._record_document_identity(document.source.uri, path, before)
        return content

    def _record_document_identity(
        self, uri: str, path: Path, before: tuple[int, int, int, int] | None
    ) -> None:
        """Keep the file's identity only when the read saw one state throughout."""
        if before is None or _file_identity(path) != before:
            return
        with self._lock:
            self._document_identities[uri] = before

    def _changed_replacement(
        self,
        document: OpenDocument,
        entry: object,
        verified: bytes | None,
        *,
        deadline: float,
    ) -> tuple[OpenDocument, dict[str, object]]:
        """The next version of a changed document and its didChange params."""
        content = verified
        if content is None:
            content = read_stable_bytes(
                document.source.absolute_path,
                MAX_DOCUMENT_BYTES,
                label="Pyright changed source document",
                deadline=deadline,
            )
        text = content.decode("utf-8", errors="strict")
        digest = hashlib.sha256(content).hexdigest()
        _check_changed_entry(entry, digest, content)
        replacement = OpenDocument(
            document.source,
            content,
            digest,
            document.version + 1,
        )
        change_params: dict[str, object] = {
            "textDocument": {
                "uri": document.source.uri,
                "version": replacement.version,
            },
            "contentChanges": [{"text": text}],
        }
        _check_encodable("textDocument/didChange", change_params)
        return replacement, change_params

    def _planned_changes(
        self,
        snapshot: _SyncSnapshot,
        delta: WorkspaceDelta,
        entries: Mapping[str, object],
        verified: Mapping[str, bytes],
        *,
        deadline: float,
    ) -> tuple[list[dict[str, object]], list[tuple[OpenDocument, OpenDocument, dict[str, object]]]]:
        """Watched-file events and the replacements for open changed documents."""
        watched = [
            {"uri": self._watched_uri(path), "type": 1} for path in delta.created
        ]
        replacements: list[
            tuple[OpenDocument, OpenDocument, dict[str, object]]
        ] = []
        for path in delta.changed:
            document = snapshot.open_by_path.get(path)
            if document is None:
                watched.append({"uri": self._watched_uri(path), "type": 2})
                continue
            replacement, params = self._changed_replacement(
                document, entries.get(path), verified.get(path), deadline=deadline
            )
            replacements.append((document, replacement, params))
        return watched, replacements

    def _planned_closes(
        self,
        snapshot: _SyncSnapshot,
        delta: WorkspaceDelta,
        watched: list[dict[str, object]],
    ) -> list[OpenDocument]:
        """Documents to close, adding the watched-file events they imply."""
        closed: list[OpenDocument] = []
        closed_uris: set[str] = set()
        for path in delta.deleted:
            _collect_closed(snapshot.open_by_path.get(path), closed, closed_uris)
            watched.append({"uri": self._watched_uri(path), "type": 3})
        for old_path, new_path in delta.renamed:
            _collect_closed(snapshot.open_by_path.get(old_path), closed, closed_uris)
            watched.append({"uri": self._watched_uri(old_path), "type": 3})
            watched.append({"uri": self._watched_uri(new_path), "type": 1})
        return closed

    def _synchronize_plan(
        self, revision: WorkspaceRevision, *, deadline: float
    ) -> tuple[SyncPlan, WorkspaceDelta]:
        """Everything this pass will do, worked out before the server is told."""
        snapshot = self._synchronize_snapshot()
        entries = {entry.path: entry for entry in revision.entries}
        delta = self._synchronize_delta(snapshot, revision, entries)
        closing_paths = set(delta.deleted)
        closing_paths.update(old_path for old_path, _new_path in delta.renamed)
        projected_bytes = self._projected_document_bytes(
            snapshot, entries, closing_paths
        )
        verified = self._verified_retained_contents(
            snapshot, entries, closing_paths, deadline=deadline
        )
        watched, replacements = self._planned_changes(
            snapshot, delta, entries, verified, deadline=deadline
        )
        closed = self._planned_closes(snapshot, delta, watched)
        plan = SyncPlan(
            process=snapshot.process,
            generation=snapshot.generation,
            prior=snapshot.prior,
            revision=revision,
            deadline=deadline,
            documents_snapshot=snapshot.documents_snapshot,
            next_documents=_next_documents(
                snapshot.documents_snapshot, closed, replacements
            ),
            projected_document_bytes=projected_bytes,
            closed_documents=closed,
            changed_replacements=replacements,
            close_notifications=_close_notifications(closed),
            watched_params=_watched_params(watched),
        )
        return plan, delta
