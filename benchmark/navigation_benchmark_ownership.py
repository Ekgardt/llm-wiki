"""The real Pyright navigation runtime and the probes that measure its lifecycle ownership."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from code_intelligence import Capability, PositionEncoding  # noqa: E402
from code_navigation import (  # noqa: E402
    CodeNavigation,
    NavigationRequest,
    NavigationResult,
    NavigationStatus,
)
from generate_python_qualification import QualificationRepository  # noqa: E402
from lsp_positions import SourceDocument  # noqa: E402
from lsp_process import coordinator_has_ownership  # noqa: E402
from lsp_protocol import CancellationSource, RequestCancelled  # noqa: E402
from navigation_benchmark_deadlines import _fresh_cleanup_deadline  # noqa: E402
from pyright_profile import PyrightIdentity  # noqa: E402
from pyright_session import PyrightSession  # noqa: E402
from repository_scope import RepositoryScope  # noqa: E402
from workspace_revision import compute_workspace_revision  # noqa: E402

_OWNERSHIP_TIMEOUT_SECONDS = 0.05
_OWNERSHIP_POLL_SECONDS = 0.001
_OWNERSHIP_PROBE_METHOD = "workspace/symbol"
# A cancellation probe only measures something when the request is still in
# flight. A fast machine can answer first, so give the probe a few tries.
_OWNERSHIP_PROBE_ATTEMPTS = 5
_OWNERSHIP_RESET_SECONDS = 60.0
_OWNERSHIP_SCENARIOS = (
    "normal_shutdown",
    "crash",
    "timeout",
    "cancellation",
)


class _CleanupProofError(RuntimeError):
    """Raised when zero retained ownership cannot be proven after one retry."""


class _ProbeRacedError(RuntimeError):
    """Raised when the server answered before the probe could interrupt it."""


class _OwnershipProbeError(RuntimeError):
    """An ownership attempt that measured nothing, under a code of its own.

    Four places raised a bare `RuntimeError`, so the report could not tell them
    apart (audit 2026-09-27 B-8).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ProbeReplyDelivery:
    """Defer only this probe's real reply; never block the reader or invent a terminal."""

    _NAMES = ("_queue_request_message", "_handle_response")

    def __init__(self, protocol: object) -> None:
        self.protocol = protocol
        self.nonce = protocol.generation_nonce
        self.queue = protocol._queue_request_message
        self.response = protocol._handle_response
        self.previous = {name: vars(protocol).get(name) for name in self._NAMES}
        self.present = {name: name in vars(protocol) for name in self._NAMES}
        self.lock = threading.Lock()
        self.released = threading.Event()
        self.key = None
        self.deferred = []
        self.reply_count = 0

    def __enter__(self):
        if getattr(self.protocol, "_benchmark_probe_delivery", None) is not None:
            raise _OwnershipProbeError("overlapping_probe", "ownership reply scopes overlap")
        self.protocol._benchmark_probe_delivery = self
        try:
            self.protocol._queue_request_message = self._queue
            self.protocol._handle_response = self._response
        except BaseException:
            self._restore()
            raise
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            self._restore()
        finally:
            self._release()

    def _restore_name(self, name):
        if self.present[name]:
            setattr(self.protocol, name, self.previous[name])
            return
        vars(self.protocol).pop(name, None)

    def _restore(self):
        for name in self._NAMES:
            self._restore_name(name)
        assert self.protocol._benchmark_probe_delivery is self
        del self.protocol._benchmark_probe_delivery

    def _remember(self, message, key):
        if not _is_ownership_probe(message):
            return
        with self.lock:
            if self.key is not None:
                raise _OwnershipProbeError("duplicate_probe", "ownership scope queued multiple probes")
            assert key == (self.nonce, message["id"])
            self.key = key

    def _queue(self, message, *, deadline, key):
        self._remember(message, key)
        return self.queue(message, deadline=deadline, key=key)

    def _hold(self, message, nonce):
        with self.lock:
            if self.released.is_set() or (nonce, message["id"]) != self.key:
                return False
            self.deferred.append((message, nonce))
            return True

    def _response(self, message, nonce):
        if self._hold(message, nonce):
            return
        return self.response(message, nonce)

    def _release(self):
        with self.lock:
            self.released.set()
            replies = tuple(self.deferred)
            self.deferred.clear()
        self.reply_count = len(replies)
        for message, nonce in replies:
            self.response(message, nonce)
        if self.reply_count > 1:
            raise _OwnershipProbeError("duplicate_response", "ownership probe received duplicate replies")


def _is_ownership_probe(message):
    return (
        isinstance(message, dict)
        and message.get("method") == _OWNERSHIP_PROBE_METHOD
        and message.get("params") == {"query": "__llm_wiki_ownership_probe_no_match__"}
    )


# The resolution of the clock every probe deadline is read on. Python 3.10 on
# Windows reads `time.monotonic()` from GetTickCount64, about 15.6 ms, against a
# 50 ms probe budget; each unmeasured attempt's message carries it, so a failure
# says whether the clock was that coarse. See
# docs/research/2026-09-27-an-ownership-failure-says-what-and-when.md.
_MONOTONIC_RESOLUTION_MS = time.get_clock_info("monotonic").resolution * 1000.0


def _unmeasured_reason(error: BaseException) -> str:
    """Why an attempt measured nothing, as the report names it.

    A Windows CI run (2026-09-26, run 36252530355) reported the timeout scenario
    unavailable and said nothing more; the next one names its cause. Research:
    docs/research/2026-09-26-an-ownership-probe-says-why-it-measured-nothing.md
    """
    if isinstance(error, _ProbeRacedError):
        return "raced"
    cause = error.__cause__
    name = _error_name(error)
    return name if cause is None else f"{name}:{type(cause).__name__}"


def _error_name(error: BaseException) -> str:
    if isinstance(error, _OwnershipProbeError):
        return error.code
    return type(error).__name__


def _unmeasured_message(error: BaseException, elapsed_seconds: float) -> str:
    """What the failure said, how long the attempt ran, and how fine the clock was."""
    cause = error.__cause__
    said = str(error) if cause is None else f"{error}: {cause}"
    timing = f"after {elapsed_seconds * 1000.0:.1f} ms; monotonic resolution {_MONOTONIC_RESOLUTION_MS:.1f} ms"
    return f"{said} ({timing})"


def _wait_one_poll(completed: threading.Event, deadline: float) -> None:
    """Yield for one poll interval, whether or not the request has finished."""
    wait_for = min(_OWNERSHIP_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
    if completed.is_set():
        time.sleep(wait_for)
        return
    completed.wait(wait_for)


def _check_probe_error(error: BaseException, scenario: str) -> None:
    """The probe has to end in the terminal its scenario asked for."""
    expected = TimeoutError if scenario == "timeout" else RequestCancelled
    if isinstance(error, expected):
        return
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise error
    raise _OwnershipProbeError("wrong_terminal", "ownership probe reached the wrong terminal") from error


def _require_single_terminal(outcome: list[tuple[str, object]], dispatched: bool) -> None:
    if not dispatched:
        raise _OwnershipProbeError("not_sent", "ownership probe request was not sent")
    if len(outcome) != 1:
        raise _OwnershipProbeError(
            "no_single_terminal", f"ownership probe ended with {len(outcome)} terminals, not one"
        )


def _check_probe_terminal(
    outcome: list[tuple[str, object]], scenario: str, dispatched: bool
) -> None:
    """What the probe ended with, refusing anything that measured nothing."""
    _require_single_terminal(outcome, dispatched)
    if outcome[0][0] != "error":
        # The server answered inside the window between dispatch and the
        # interruption. Nothing was in flight to interrupt, so this attempt
        # measured nothing; the caller retries rather than calling the
        # scenario unavailable.
        raise _ProbeRacedError("ownership probe was answered before interruption")
    _check_probe_error(outcome[0][1], scenario)


def _probe_request_deadline(scenario: str, now: float, deadline: float) -> float:
    """The timeout scenario gets a short budget; the others get the full one."""
    if scenario != "timeout":
        return deadline
    return min(deadline, now + _OWNERSHIP_TIMEOUT_SECONDS)


def _process_alive(popen: object) -> bool:
    return popen is not None and popen.poll() is None


def _coordinator_owns(coordinator: object) -> bool:
    return coordinator is not None and coordinator_has_ownership(coordinator)


def _lease_exists(lease: Path | None) -> bool:
    return lease is not None and lease.exists()


def _process_handles(process: object) -> tuple[object, object, Path | None]:
    """(popen, coordinator, lease path) of a live process; Nones when there is none."""
    if process is None:
        return None, None, None
    return process.process, process._coordinator, process.owner_root / "lease.json"


def _orphan_evidence_problem(orphan: object) -> str | None:
    if isinstance(orphan, bool) or orphan not in {0, 1}:
        return "runtime cleanup returned invalid evidence"
    if orphan:
        return "runtime cleanup reported retained ownership"
    return None


def _retry_once_after_oserror(retried: bool, deadline: float) -> bool:
    """True once: a second OSError, or one past the deadline, propagates."""
    if retried or time.monotonic() >= deadline:
        raise
    return True


def _still_not_ready(recovered: object, deadline: float) -> bool:
    if not isinstance(recovered, NavigationResult):
        return False
    return recovered.status is NavigationStatus.NOT_READY and time.monotonic() < deadline


class _RealNavigationRuntime:
    """Own the real PyrightSession and CodeNavigation lifecycle."""

    def __init__(
        self,
        repository: QualificationRepository,
        scope: RepositoryScope,
        identity: PyrightIdentity,
        state_root: Path,
    ) -> None:
        self.repository = repository
        self.scope = scope
        self.identity = identity
        self.state_root = state_root
        self._session: PyrightSession
        self._navigation: CodeNavigation
        self._last_request: NavigationRequest | None = None
        self._cleanup_failed = False
        self._open()

    def _open(self) -> None:
        self._session = PyrightSession(
            self.scope,
            self.identity,
            state_root=self.state_root,
        )
        self._navigation = CodeNavigation(
            self.scope,
            self._session,
            self.identity,
        )

    def query(self, request: NavigationRequest, *, deadline: float) -> NavigationResult:
        self._last_request = request
        return self._navigation.query(request, deadline=deadline)

    def synchronize(self, *, deadline: float) -> None:
        revision = compute_workspace_revision(self.scope, deadline=deadline)
        self._session.synchronize(revision, deadline=deadline)

    def direct_query(self, request: NavigationRequest, *, deadline: float) -> object:
        revision = compute_workspace_revision(self.scope, deadline=deadline)
        self._session.synchronize(revision, deadline=deadline)
        content = (self.repository.root / request.path).read_bytes()
        anchor = SourceDocument.from_bytes(request.path, content).validate_anchor(
            line=request.line,
            character=request.character,
        )
        if request.capability is Capability.DEFINITIONS:
            return self._session.definition(anchor, deadline=deadline)
        if request.capability is Capability.REFERENCES:
            return self._session.references(anchor, deadline=deadline)
        return self._direct_calls(request, anchor, deadline)

    def _direct_calls(self, request: NavigationRequest, anchor: object, deadline: float) -> object:
        if request.capability is not Capability.CALLS:
            raise ValueError("unsupported direct qualification capability")
        if request.direction == "incoming":
            return self._session.incoming_calls(anchor, deadline=deadline)
        return self._session.outgoing_calls(anchor, deadline=deadline)

    @property
    def position_encoding(self) -> PositionEncoding | None:
        return self._session.position_encoding

    def _close_and_count(self, deadline: float) -> int:
        popen, coordinator, lease = _process_handles(self._session._process)
        self._navigation.close(deadline=deadline)
        return int(_process_alive(popen) or _coordinator_owns(coordinator) or _lease_exists(lease))

    @property
    def cleanup_failed(self) -> bool:
        return bool(getattr(self, "_cleanup_failed", False))

    def _retry_retained_cleanup(self) -> None:
        try:
            retried = self._close_and_count(_fresh_cleanup_deadline())
        except Exception:
            return
        if isinstance(retried, bool) or retried not in {0, 1}:
            return

    def _fail_cleanup(self) -> None:
        self._cleanup_failed = True
        self._retry_retained_cleanup()

    def _reset(self, deadline: float) -> int:
        if self.cleanup_failed:
            raise _CleanupProofError("runtime cleanup is terminal")
        try:
            orphan = self._close_and_count(deadline)
        except Exception as exc:
            self._fail_cleanup()
            raise _CleanupProofError("runtime cleanup could not be proven") from exc
        problem = _orphan_evidence_problem(orphan)
        if problem is not None:
            self._fail_cleanup()
            raise _CleanupProofError(problem)
        self._open()
        return 0

    def crash_and_recover(
        self,
        request: NavigationRequest,
        *,
        deadline: float,
    ) -> NavigationResult | None:
        if self.cleanup_failed:
            return None
        self.query(request, deadline=deadline)
        process = self._session._process
        if process is None:
            return None
        try:
            process.process.kill()
            return self._query_after_crash(request, deadline)
        finally:
            self._reset(deadline)

    def _query_after_crash(
        self,
        request: NavigationRequest,
        deadline: float,
    ) -> NavigationResult:
        retried_oserror = False
        while True:
            try:
                recovered = self.query(request, deadline=deadline)
            except OSError:
                retried_oserror = _retry_once_after_oserror(retried_oserror, deadline)
                continue
            if not _still_not_ready(recovered, deadline):
                return recovered

    def _prepare_process(self, deadline: float):
        request = self._last_request
        if request is None:
            raise _OwnershipProbeError("no_prior_request", "ownership probe requires a prior navigation request")
        self.query(request, deadline=deadline)
        process = self._session._process
        if process is None:
            raise _OwnershipProbeError("no_live_process", "ownership probe has no live process")
        return request, process

    def _start_probe_request(
        self,
        process: object,
        scenario: str,
        request_deadline: float,
        source: CancellationSource,
        outcome: list[tuple[str, object]],
        completed: threading.Event,
    ) -> threading.Thread:
        """Send the probe request from a worker so the caller can interrupt it."""

        def request_workspace_symbols() -> None:
            try:
                result = process.request(
                    _OWNERSHIP_PROBE_METHOD,
                    {"query": "__llm_wiki_ownership_probe_no_match__"},
                    deadline=request_deadline,
                    cancellation=source.token,
                )
            except BaseException as exc:
                outcome.append(("error", exc))
            else:
                outcome.append(("result", result))
            finally:
                completed.set()

        worker = threading.Thread(
            target=request_workspace_symbols,
            name=f"code-navigation-ownership-{scenario}",
            daemon=True,
        )
        worker.start()
        return worker

    def _await_probe_dispatch(
        self,
        protocol: object,
        baseline_sequence: int,
        completed: threading.Event,
        deadline: float,
    ) -> bool:
        """Whether the probe request reached the wire before the deadline."""
        while time.monotonic() < deadline:
            sent_sequence, sent_method = protocol._sent_request_evidence()
            if (
                sent_sequence > baseline_sequence
                and sent_method == _OWNERSHIP_PROBE_METHOD
            ):
                return True
            _wait_one_poll(completed, deadline)
        return False

    def _join_probe_worker(
        self, worker: threading.Thread, completed: threading.Event, deadline: float
    ) -> None:
        """The probe owns a thread and a request; neither may outlive the probe."""
        if not completed.wait(max(0.0, deadline - time.monotonic())):
            self._cleanup_failed = True
            raise _CleanupProofError("ownership probe request did not terminate")
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            self._cleanup_failed = True
            raise _CleanupProofError("ownership probe request owner did not stop")

    def _observe_inflight_interruption(
        self,
        process: object,
        scenario: str,
        deadline: float,
    ) -> None:
        delivery = _ProbeReplyDelivery(process.protocol)
        try:
            with delivery:
                self._interrupt_probe(process, scenario, deadline)
        finally:
            self._ownership_record("ownership_reply_faults")[scenario] = {
                "mode": "deferred_exact_probe_reply",
                "request_key": delivery.key,
                "reply_count": delivery.reply_count,
                "released": delivery.released.is_set(),
            }

    def _interrupt_probe(
        self,
        process: object,
        scenario: str,
        deadline: float,
    ) -> None:
        protocol = process.protocol
        baseline_sequence, _baseline_method = protocol._sent_request_evidence()
        source = CancellationSource()
        now = time.monotonic()
        request_deadline = _probe_request_deadline(scenario, now, deadline)
        if now >= request_deadline:
            raise TimeoutError("ownership probe deadline expired")

        completed = threading.Event()
        outcome: list[tuple[str, object]] = []
        worker = self._start_probe_request(
            process, scenario, request_deadline, source, outcome, completed
        )
        dispatched = self._await_probe_dispatch(
            protocol, baseline_sequence, completed, deadline
        )
        if dispatched and scenario == "cancellation":
            source.cancel()
        self._join_probe_worker(worker, completed, deadline)
        _check_probe_terminal(outcome, scenario, dispatched)

    def ownership_checks(self, *, deadline: float) -> dict[str, dict[str, int | bool | None]]:
        outcomes: dict[str, dict[str, int | bool | None]] = {
            scenario: {"available": False, "orphan_count": None}
            for scenario in _OWNERSHIP_SCENARIOS
        }
        self.ownership_reasons = {}
        self.ownership_messages = {}
        self.ownership_recovery = {}
        for scenario in _OWNERSHIP_SCENARIOS:
            if self.cleanup_failed:
                break
            outcomes[scenario] = self._ownership_outcome(scenario, deadline)
        return outcomes

    def _can_attempt_ownership(self, deadline: float) -> bool:
        return not self.cleanup_failed and time.monotonic() < deadline

    def _attempt_ownership_scenario(
        self, scenario: str, deadline: float
    ) -> tuple[int | None, bool]:
        """The retained-owner count, or None and whether a retry is worthwhile."""
        started = time.perf_counter()
        try:
            return self._run_ownership_scenario(scenario, deadline), False
        except Exception as error:
            self._note_unmeasured(scenario, error, time.perf_counter() - started)
            self._recover_ownership_scenario(scenario)
            return None, isinstance(error, _ProbeRacedError)

    def _ownership_record(self, name: str) -> dict[str, str]:
        """One of the per-scenario records the report reads, created on first use."""
        record = getattr(self, name, None)
        if record is None:
            record = {}
            setattr(self, name, record)
        return record

    def _note_unmeasured(self, scenario: str, error: BaseException, elapsed_seconds: float) -> None:
        """Keep the last reason a scenario measured nothing, and what it said, for the report."""
        self._ownership_record("ownership_reasons")[scenario] = _unmeasured_reason(error)
        self._ownership_record("ownership_messages")[scenario] = _unmeasured_message(error, elapsed_seconds)

    def _ownership_attempt_or_stop(self, scenario: str, deadline: float) -> tuple[int | None, bool]:
        """One attempt, or (None, False) when no attempt may start."""
        if not self._can_attempt_ownership(deadline):
            return None, False
        return self._attempt_ownership_scenario(scenario, deadline)

    def _ownership_outcome(
        self, scenario: str, deadline: float
    ) -> dict[str, int | bool | None]:
        """One scenario's result, retrying attempts that measured nothing."""
        for _attempt in range(_OWNERSHIP_PROBE_ATTEMPTS):
            orphan, retry = self._ownership_attempt_or_stop(scenario, deadline)
            if orphan is not None:
                return {"available": True, "orphan_count": orphan}
            if not retry:
                break
        return {"available": False, "orphan_count": None}

    def _recover_ownership_scenario(self, scenario: str) -> None:
        """Put the harness back where the next attempt can start, or record why it could not.

        A failed reset used to be swallowed; the next attempt then started from an
        unknown state and its failure was the only one the report named.
        """
        if self.cleanup_failed:
            return
        started = time.perf_counter()
        try:
            self._reset(time.monotonic() + _OWNERSHIP_RESET_SECONDS)
        except Exception as error:
            elapsed = time.perf_counter() - started
            self._ownership_record("ownership_recovery")[scenario] = (
                _unmeasured_reason(error), _unmeasured_message(error, elapsed)
            )

    def _run_ownership_scenario(self, scenario: str, deadline: float) -> int:
        """Drive one scenario to its terminal and return the retained owners."""
        request, process = self._prepare_process(deadline)
        if scenario == "crash":
            process.process.kill()
            self._query_after_crash(request, deadline)
            return self._reset(deadline)
        if scenario in {"timeout", "cancellation"}:
            self._observe_inflight_interruption(process, scenario, deadline)
        return self._reset(deadline)

    def close(self, *, deadline: float) -> int:
        return self._close_and_count(deadline)
