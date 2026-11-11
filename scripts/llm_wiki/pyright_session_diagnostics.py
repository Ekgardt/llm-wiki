"""Published diagnostics: parsed, bounded per session and answered for one open document."""

import dataclasses
import time
from collections.abc import Mapping

from llm_wiki.pyright_session_ranges import bounded_text, lsp_coordinate, lsp_range
from llm_wiki.pyright_session_values import (
    DiagnosticSnapshot,
    LspDiagnostic,
    LspLocation,
    OpenDocument,
    ProviderDiagnostics,
    validated_deadline,
)
from lsp_positions import LspRange
from lsp_process import LspProcess
from lsp_protocol import MAX_LOCATIONS
from lsp_security import normalize_provider_uri

# A diagnostic message: 64 KiB, one of the text fields of a server answer; a longer one makes
# that diagnostic unreadable. Value predates measurement.
_MAX_DIAGNOSTIC_TEXT_BYTES = 64 * 1024
# Diagnostics retained per session: 256 files and 16 MiB in all, each item costed with a fixed
# overhead (128 bytes, 96 per related entry). A publication past them keeps the snapshot
# held for its version, or an empty one, marked `partial`, so the query answers at once
# and says it is incomplete. Values predate measurement; review if diagnostics go missing
# on a large repository.
_MAX_DIAGNOSTIC_URIS = 256
MAX_DIAGNOSTIC_BYTES = 16 * 1024 * 1024
_DIAGNOSTIC_BASE_BYTES = 128
_DIAGNOSTIC_RELATED_BASE_BYTES = 96


_DIAGNOSTIC_SEVERITIES = frozenset({1, 2, 3, 4})


def _known_severity(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value in _DIAGNOSTIC_SEVERITIES


def _diagnostic_severity(value: object) -> tuple[int | None, bool]:
    """The severity, and whether the field was readable at all."""
    if value is None:
        return None, True
    if not _known_severity(value):
        return None, False
    return value, True


def _diagnostic_int_code(value: object) -> tuple[str | None, bool]:
    """A numeric diagnostic code rendered as text, when it fits in 32 bits."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None, False
    if not -(2**31) <= value <= 2**31 - 1:
        return None, False
    return str(value), True


def _diagnostic_code(value: object) -> tuple[str | None, bool]:
    """The code as text, and whether the field was readable at all."""
    if value is None:
        return None, True
    if isinstance(value, str):
        code = bounded_text(value, 4096)
        return code, code is not None
    return _diagnostic_int_code(value)


def drop_superseded_diagnostics(
    diagnostics: dict[str, DiagnosticSnapshot],
    document: OpenDocument,
    replacement: OpenDocument,
) -> None:
    """A snapshot older than the replacement no longer describes the document."""
    snapshot = diagnostics.get(document.source.uri)
    if snapshot is None or snapshot.document_version is None:
        return
    if snapshot.document_version < replacement.version:
        diagnostics.pop(document.source.uri, None)


def _diagnostic_classification(
    value: Mapping[str, object],
) -> tuple[int | None, str | None] | None:
    """Severity and code together, or None when either is present but unreadable."""
    severity, severity_ok = _diagnostic_severity(value.get("severity"))
    if not severity_ok:
        return None
    code, code_ok = _diagnostic_code(value.get("code"))
    if not code_ok:
        return None
    return severity, code


def _related_message(
    location: LspLocation, raw_message: object
) -> tuple[LspLocation, str | None] | None:
    """A related entry keeps its location; an unreadable message drops the entry."""
    if raw_message is None:
        return location, None
    message = bounded_text(raw_message, _MAX_DIAGNOSTIC_TEXT_BYTES)
    if message is None:
        return None
    return location, message


def _versioned_diagnostic_target(
    uri: str, values: list[object], version_value: object
) -> tuple[str, list[object], int | None] | None:
    version, version_ok = _published_version(version_value)
    if not version_ok:
        return None
    return uri, values, version


def _published_diagnostics_params(
    params: object,
) -> tuple[str, list[object], object] | None:
    """The URI, diagnostics and version of a publish notification, when readable."""
    if not isinstance(params, dict):
        return None
    uri_value = params.get("uri")
    diagnostics_value = params.get("diagnostics")
    if not isinstance(uri_value, str) or not isinstance(diagnostics_value, list):
        return None
    return uri_value, diagnostics_value, params.get("version")


def _published_version(value: object) -> tuple[int | None, bool]:
    """The document version, and whether the field was readable at all."""
    if value is None:
        return None, True
    version = lsp_coordinate(value)
    return version, version is not None


def _matching_diagnostics(
    snapshot: DiagnosticSnapshot | None, version: int
) -> ProviderDiagnostics | None:
    """The snapshot, when it describes exactly this document version."""
    if snapshot is None or snapshot.document_version != version:
        return None
    return ProviderDiagnostics(
        snapshot.diagnostics,
        snapshot.document_version,
        snapshot.partial,
    )


def _over_budget_replacement(
    existing: DiagnosticSnapshot | None, version: int | None
) -> DiagnosticSnapshot:
    """What a publication past the diagnostics budget leaves, marked `partial`.

    The snapshot already held for the same version stays, now saying it may be
    incomplete; otherwise an empty marker of that version. A marker costs one fixed
    item, so all open documents' markers stay within
    `MAX_OPEN_DOCUMENTS * _DIAGNOSTIC_BASE_BYTES` (32 KiB) above the budget.
    """
    if existing is not None and existing.document_version == version:
        return dataclasses.replace(existing, partial=True)
    return DiagnosticSnapshot((), version, True, _DIAGNOSTIC_BASE_BYTES)


def _expired_diagnostics(
    snapshot: DiagnosticSnapshot | None,
) -> ProviderDiagnostics:
    """What to answer when the wait ran out: a versionless snapshot, or nothing."""
    if snapshot is not None and snapshot.document_version is None:
        return ProviderDiagnostics(snapshot.diagnostics, None, True)
    return ProviderDiagnostics((), None, True)


def _snapshot_supersedes(
    existing: DiagnosticSnapshot | None, version: int | None
) -> bool:
    """The snapshot already describes a newer version than the update does."""
    if existing is None or existing.document_version is None:
        return False
    if version is None:
        return True
    return version < existing.document_version


class SessionDiagnostics:
    """Store published diagnostics and answer the diagnostics query."""

    def _diagnostic_core(
        self, value: Mapping[str, object]
    ) -> tuple[LspRange, str, int | None, str | None] | None:
        """Range, message, severity and code, or None when one is unreadable."""
        range_ = lsp_range(value.get("range"))
        message = bounded_text(value.get("message"), _MAX_DIAGNOSTIC_TEXT_BYTES)
        if range_ is None or message is None:
            return None
        classified = _diagnostic_classification(value)
        if classified is None:
            return None
        severity, code = classified
        return range_, message, severity, code

    def _related_entry(
        self, relation: object
    ) -> tuple[LspLocation, str | None] | None:
        """One related location, or None when it cannot be read."""
        if not isinstance(relation, dict):
            return None
        location = self._normalize_location(relation.get("location"))
        if location is None:
            return None
        return _related_message(location, relation.get("message"))

    def _related_information(
        self, value: object
    ) -> tuple[list[tuple[LspLocation, str | None]], bool]:
        """The related locations, and whether anything had to be dropped."""
        if value is None:
            return [], False
        if not isinstance(value, list):
            return [], True
        related, dropped = self._related_entries(value[:MAX_LOCATIONS])
        return related, len(value) > MAX_LOCATIONS or dropped

    def _related_entries(
        self, relations: list[object]
    ) -> tuple[list[tuple[LspLocation, str | None]], bool]:
        """The entries that parse, and whether any did not."""
        entries = [self._related_entry(relation) for relation in relations]
        related = [entry for entry in entries if entry is not None]
        return related, len(related) != len(entries)

    def _parse_diagnostic(
        self,
        value: object,
        uri: str,
    ) -> tuple[LspDiagnostic | None, bool]:
        if not isinstance(value, dict):
            return None, True
        core = self._diagnostic_core(value)
        if core is None:
            return None, True
        range_, message, severity, code = core
        related, partial = self._related_information(value.get("relatedInformation"))
        diagnostic = LspDiagnostic(
            uri,
            range_,
            severity,
            code,
            message,
            tuple(related),
        )
        return diagnostic, partial

    @staticmethod
    def _diagnostic_retained_bytes(diagnostic: LspDiagnostic) -> int:
        size = (
            _DIAGNOSTIC_BASE_BYTES
            + len(diagnostic.uri.encode("utf-8", errors="strict"))
            + len(diagnostic.message.encode("utf-8", errors="strict"))
        )
        if diagnostic.code is not None:
            size += len(diagnostic.code.encode("utf-8", errors="strict"))
        for location, message in diagnostic.related:
            size += _DIAGNOSTIC_RELATED_BASE_BYTES
            size += len(location.uri.encode("utf-8", errors="strict"))
            if message is not None:
                size += len(message.encode("utf-8", errors="strict"))
        return size

    @staticmethod
    def _diagnostic_update_is_stale(
        document: OpenDocument,
        existing: DiagnosticSnapshot | None,
        version: int | None,
    ) -> bool:
        if version is not None and version < document.version:
            return True
        return _snapshot_supersedes(existing, version)

    def _diagnostic_update_admissible_locked(
        self, uri: str, version: int | None
    ) -> bool:
        """Whether a diagnostic update for this URI is still worth keeping."""
        document = self._documents.get(uri)
        if document is None:
            return False
        existing = self._diagnostics.get(uri)
        if self._diagnostic_update_is_stale(document, existing, version):
            return False
        return existing is not None or len(self._diagnostics) < _MAX_DIAGNOSTIC_URIS

    def _parsed_diagnostics(
        self, values: list[object], uri: str
    ) -> tuple[list[LspDiagnostic], bool]:
        """Every readable diagnostic, and whether anything had to be dropped."""
        partial = len(values) > MAX_LOCATIONS
        diagnostics: list[LspDiagnostic] = []
        for value in values[:MAX_LOCATIONS]:
            diagnostic, filtered = self._parse_diagnostic(value, uri)
            partial = partial or filtered
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return diagnostics, partial

    def _diagnostic_snapshot(
        self, values: list[object], uri: str, version: int | None
    ) -> DiagnosticSnapshot:
        """The snapshot as published; `_store_diagnostics` decides whether it fits."""
        diagnostics, partial = self._parsed_diagnostics(values, uri)
        retained_bytes = _DIAGNOSTIC_BASE_BYTES + sum(
            self._diagnostic_retained_bytes(diagnostic) for diagnostic in diagnostics
        )
        return DiagnosticSnapshot(
            tuple(diagnostics),
            version,
            partial,
            retained_bytes,
        )

    def _aggregate_after_locked(self, uri: str, snapshot: DiagnosticSnapshot) -> int:
        existing = self._diagnostics.get(uri)
        previous_bytes = existing.retained_bytes if existing is not None else 0
        return self._diagnostic_bytes - previous_bytes + snapshot.retained_bytes

    def _store_diagnostics(
        self, uri: str, version: int | None, snapshot: DiagnosticSnapshot
    ) -> None:
        """Publish the snapshot, or what the budget allows, unless the session moved on.

        A publication past the budget used to be dropped without a trace, so the
        query for that version waited out its whole deadline and then answered
        an unexplained partial. Now it is answered at once, marked `partial`.
        """
        with self._lock:
            if not self._diagnostic_update_admissible_locked(uri, version):
                return
            snapshot = self._fitting_snapshot_locked(uri, snapshot)
            self._diagnostic_bytes = self._aggregate_after_locked(uri, snapshot)
            self._diagnostics[uri] = snapshot
            self._condition.notify_all()

    def _fitting_snapshot_locked(
        self, uri: str, snapshot: DiagnosticSnapshot
    ) -> DiagnosticSnapshot:
        if self._aggregate_after_locked(uri, snapshot) <= MAX_DIAGNOSTIC_BYTES:
            return snapshot
        return _over_budget_replacement(self._diagnostics.get(uri), snapshot.document_version)

    def _diagnostic_target(
        self, params: object
    ) -> tuple[str, list[object], int | None] | None:
        """The URI, diagnostics and version to publish, when all are readable."""
        published = _published_diagnostics_params(params)
        if published is None:
            return None
        uri_value, values, version_value = published
        source = normalize_provider_uri(self._repository, uri_value)
        if source is None:
            return None
        return _versioned_diagnostic_target(source.uri, values, version_value)

    def _publish_diagnostics(self, params: object) -> None:
        target = self._diagnostic_target(params)
        if target is None:
            return
        uri, values, version = target
        with self._lock:
            if not self._diagnostic_update_admissible_locked(uri, version):
                return
        self._store_diagnostic_snapshot(uri, values, version)

    def _store_diagnostic_snapshot(
        self, uri: str, values: list[object], version: int | None
    ) -> None:
        self._store_diagnostics(uri, version, self._diagnostic_snapshot(values, uri, version))

    def _diagnostics_ready(self, epoch: int) -> bool:
        """The session can answer for diagnostics at this synchronize epoch."""
        with self._lock:
            initialized = (
                self._process is not None and self._position_encoding is not None
            )
            current = self._semantic_query_epoch_current_locked(epoch)
        return initialized and current

    def _await_diagnostics_locked(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation: str,
        epoch: int,
        deadline: float,
    ) -> ProviderDiagnostics:
        """Wait for a snapshot of this document version; the caller holds the lock."""
        while True:
            settled = self._settled_diagnostics_locked(
                document, process, generation, epoch, deadline
            )
            if settled is not None:
                return settled
            self._condition.wait(deadline - time.monotonic())

    def _settled_diagnostics_locked(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation: str,
        epoch: int,
        deadline: float,
    ) -> ProviderDiagnostics | None:
        """The answer this wait can already give, or None to keep waiting."""
        if not self._document_query_current_locked(
            process, generation, document, epoch
        ):
            return ProviderDiagnostics((), None, True)
        snapshot = self._diagnostics.get(document.source.uri)
        matched = _matching_diagnostics(snapshot, document.version)
        if matched is not None:
            return matched
        return _expired_diagnostics(snapshot) if deadline <= time.monotonic() else None

    def _diagnostics_for_document(
        self, document: OpenDocument, epoch: int, deadline: float
    ) -> ProviderDiagnostics:
        with self._lock:
            process = self._process
            generation = self._generation_nonce
            if process is None or generation is None:
                return ProviderDiagnostics((), None, True)
            if not self._document_query_current_locked(
                process, generation, document, epoch
            ):
                return ProviderDiagnostics((), None, True)
            return self._await_diagnostics_locked(
                document, process, generation, epoch, deadline
            )

    def _diagnostics_within_operation(
        self, path: str, deadline: float
    ) -> ProviderDiagnostics:
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return ProviderDiagnostics((), None, True)
        self.start(deadline=deadline)
        if not self._diagnostics_ready(epoch):
            return ProviderDiagnostics((), None, True)
        document = self.open_document(path, deadline=deadline)
        return self._diagnostics_for_document(document, epoch, deadline)

    def diagnostics(self, path: str, *, deadline: float) -> ProviderDiagnostics:
        deadline = validated_deadline(deadline)
        with self._operation():
            return self._diagnostics_within_operation(path, deadline)
