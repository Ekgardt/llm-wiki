"""One `textDocument/didOpen` per document and wire generation, sent once and settled for every waiter."""

import time
from collections.abc import Callable
from pathlib import PurePosixPath

from llm_wiki.pyright_session_values import OpenDocument
from lsp_process import LspProcess
from lsp_protocol import LspProtocol


class SessionWire:
    """Send each document's `didOpen` once per wire generation."""

    def _begin_wire_generation(self, generation_nonce: str) -> None:
        with self._wire_condition:
            self._wire_generation = generation_nonce
            self._wire_opened.clear()
            self._wire_failed.clear()
            self._wire_condition.notify_all()

    def _discard_wire_generation(self, generation_nonce: str) -> None:
        with self._wire_condition:
            if self._wire_generation == generation_nonce:
                self._wire_generation = None
                self._wire_opened.clear()
                self._wire_failed.clear()
            self._wire_condition.notify_all()

    def _clear_wire_state(self) -> None:
        with self._wire_condition:
            self._wire_generation = None
            self._wire_opened.clear()
            self._wire_failed.clear()
            self._wire_condition.notify_all()

    def _wire_document_opened(
        self,
        document: OpenDocument,
        generation_nonce: str | None,
    ) -> bool:
        if generation_nonce is None:
            return False
        key = (generation_nonce, document.source.uri, document.version)
        with self._wire_condition:
            return self._wire_generation == generation_nonce and key in self._wire_opened

    def _send_did_open_once(
        self,
        document: OpenDocument,
        generation_nonce: str,
        *,
        deadline: float,
        notify: Callable[[], bool | None],
    ) -> bool:
        key = (generation_nonce, document.source.uri, document.version)
        claimed = self._claim_did_open(key, generation_nonce, deadline)
        if claimed is not None:
            return claimed
        sent = False
        try:
            sent = notify() is not False
        except BaseException:
            self._record_did_open_failure(key, generation_nonce)
            raise
        finally:
            retained = self._release_did_open(key, generation_nonce, sent=sent)
        return retained

    def _await_did_open_gate_locked(self, key: tuple, deadline: float) -> None:
        """Wait out another sender of the same key; caller holds the condition."""
        while key in self._wire_sending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._wire_condition.wait(remaining):
                raise TimeoutError("Pyright didOpen send gate deadline expired")

    def _claim_did_open(
        self, key: tuple, generation_nonce: str, deadline: float
    ) -> bool | None:
        """The settled answer for this key, or None when this call must send."""
        with self._wire_condition:
            self._await_did_open_gate_locked(key, deadline)
            settled = self._settled_did_open_locked(key, generation_nonce)
            if settled is None:
                self._wire_sending.add(key)
        return settled

    def _settled_did_open_locked(
        self, key: tuple, generation_nonce: str
    ) -> bool | None:
        """The answer already on record for this key, or None to send it."""
        if self._wire_generation != generation_nonce:
            return False
        if key in self._wire_opened:
            return True
        return False if key in self._wire_failed else None

    def _record_did_open_failure(self, key: tuple, generation_nonce: str) -> None:
        with self._wire_condition:
            if self._wire_generation == generation_nonce:
                self._wire_failed.add(key)

    def _release_did_open(
        self, key: tuple, generation_nonce: str, *, sent: bool
    ) -> bool:
        """Leave the gate, recording the open only if it still belongs to us."""
        with self._wire_condition:
            self._wire_sending.remove(key)
            retained = sent and self._wire_generation == generation_nonce
            if retained:
                self._wire_opened.add(key)
            self._wire_condition.notify_all()
        return retained

    def _did_open_params(self, document: OpenDocument) -> dict[str, object]:
        suffix = PurePosixPath(document.source.relative_path).suffix
        return {
            "textDocument": {
                "uri": document.source.uri,
                "languageId": self._profile.language_id_for(suffix),
                "version": document.version,
                "text": document.content.decode("utf-8", errors="strict"),
            }
        }

    def _send_protocol_did_open(
        self,
        document: OpenDocument,
        protocol: LspProtocol,
        generation_nonce: str,
        *,
        deadline: float,
    ) -> bool:
        params = self._did_open_params(document)
        return self._send_did_open_once(
            document,
            generation_nonce,
            deadline=deadline,
            notify=lambda: protocol.notify(
                "textDocument/didOpen",
                params,
                deadline=deadline,
            ),
        )

    def _send_process_did_open(
        self,
        document: OpenDocument,
        process: LspProcess,
        *,
        deadline: float,
    ) -> bool:
        attempted: set[str] = set()
        while True:
            generation_nonce = self._next_untried_generation(attempted)
            if generation_nonce is None:
                return False
            settled = self._did_open_generation_outcome(
                document, process, generation_nonce, deadline
            )
            if settled is not None:
                return settled

    def _did_open_generation_outcome(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation_nonce: str,
        deadline: float,
    ) -> bool | None:
        """True sent, False the generation is still current and refused, None retry."""
        if self._send_did_open_to_generation(
            document, process, generation_nonce, deadline=deadline
        ):
            return True
        if self._current_generation_nonce() == generation_nonce:
            return False
        return None

    def _next_untried_generation(self, attempted: set[str]) -> str | None:
        """The current generation, unless we have already tried it."""
        generation_nonce = self._current_generation_nonce()
        if generation_nonce is None or generation_nonce in attempted:
            return None
        attempted.add(generation_nonce)
        return generation_nonce

    def _current_generation_nonce(self) -> str | None:
        with self._lock:
            return self._generation_nonce

    def _send_did_open_to_generation(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation_nonce: str,
        *,
        deadline: float,
    ) -> bool:
        params = self._did_open_params(document)
        return self._send_did_open_once(
            document,
            generation_nonce,
            deadline=deadline,
            notify=lambda: process.notify_generation(
                "textDocument/didOpen",
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            ),
        )
