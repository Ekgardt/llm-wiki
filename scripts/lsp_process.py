"""Own one minimally privileged, bounded LSP child process."""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from llm_wiki import (
    lsp_process_cleanup,  # noqa: E402
    lsp_process_failure,  # noqa: E402
    lsp_process_limits,  # noqa: E402
)
from llm_wiki.lsp_process_caller import cancel_all_lsp_process as cancel_all_lsp_process  # noqa: E402
from llm_wiki.lsp_process_caller import restart_lsp_process as restart_lsp_process  # noqa: E402
from llm_wiki.lsp_process_caller import shutdown_lsp_process as shutdown_lsp_process  # noqa: E402
from llm_wiki.lsp_process_caller import wait_for_lsp_exit as wait_for_lsp_exit  # noqa: E402
from llm_wiki.lsp_process_cleanup import StartupCleanupError as StartupCleanupError  # noqa: E402
from llm_wiki.lsp_process_cleanup import atexit_cleanup_startups as atexit_cleanup_startups  # noqa: E402
from llm_wiki.lsp_process_cleanup import drive_cleanup as drive_cleanup  # noqa: E402
from llm_wiki.lsp_process_cleanup import retry_startup_cleanup as retry_startup_cleanup  # noqa: E402
from llm_wiki.lsp_process_cleanup import stop_stderr_drain as stop_stderr_drain  # noqa: E402
from llm_wiki.lsp_process_cleanup import (
    terminal_failure_lsp_process as terminal_failure_lsp_process,  # noqa: E402
)
from llm_wiki.lsp_process_failure import drain_terminal_failures as drain_terminal_failures  # noqa: E402
from llm_wiki.lsp_process_failure import ensure_failure_evidence as ensure_failure_evidence  # noqa: E402
from llm_wiki.lsp_process_failure import failure_stderr_tail as failure_stderr_tail  # noqa: E402
from llm_wiki.lsp_process_failure import mark_terminal_failure as mark_terminal_failure  # noqa: E402
from llm_wiki.lsp_process_failure import (
    select_terminal_failure_locked as select_terminal_failure_locked,  # noqa: E402
)
from llm_wiki.lsp_process_failure import stop_heartbeat_owner as stop_heartbeat_owner  # noqa: E402
from llm_wiki.lsp_process_failure import stop_recovery_owner as stop_recovery_owner  # noqa: E402
from llm_wiki.lsp_process_generation import active_drain_generation as active_drain_generation  # noqa: E402
from llm_wiki.lsp_process_generation import generation_guard_context as generation_guard_context  # noqa: E402
from llm_wiki.lsp_process_generation import prepare_generation as prepare_generation  # noqa: E402
from llm_wiki.lsp_process_generation import report_protocol_warning as report_protocol_warning  # noqa: E402
from llm_wiki.lsp_process_generation import write_current_lease as write_current_lease  # noqa: E402
from llm_wiki.lsp_process_generation import write_generation_lease as write_generation_lease  # noqa: E402
from llm_wiki.lsp_process_launch import DESCRIPTOR_ROOTS as DESCRIPTOR_ROOTS  # noqa: E402
from llm_wiki.lsp_process_launch import fresh_bootstrap_deadline as fresh_bootstrap_deadline  # noqa: E402
from llm_wiki.lsp_process_launch import fresh_cleanup_deadline as fresh_cleanup_deadline  # noqa: E402
from llm_wiki.lsp_process_launch import generation_launch as generation_launch  # noqa: E402
from llm_wiki.lsp_process_launch import is_inherited_descriptor as is_inherited_descriptor  # noqa: E402
from llm_wiki.lsp_process_launch import (
    reject_protocol_callback_lifecycle as reject_protocol_callback_lifecycle,  # noqa: E402
)
from llm_wiki.lsp_process_launch import require_same_program as require_same_program  # noqa: E402
from llm_wiki.lsp_process_launch import wrap_protocol_callback as wrap_protocol_callback  # noqa: E402
from llm_wiki.lsp_process_limits import GRACEFUL_CLEANUP_SECONDS as GRACEFUL_CLEANUP_SECONDS  # noqa: E402
from llm_wiki.lsp_process_limits import HEARTBEAT_SECONDS as HEARTBEAT_SECONDS  # noqa: E402
from llm_wiki.lsp_process_limits import LEASE_EXPIRY_SECONDS as LEASE_EXPIRY_SECONDS  # noqa: E402
from llm_wiki.lsp_process_limits import LSP_ENV_ALLOWLIST as LSP_ENV_ALLOWLIST  # noqa: E402
from llm_wiki.lsp_process_limits import MAX_RETAINED_CLEANUP_ERRORS as MAX_RETAINED_CLEANUP_ERRORS  # noqa: E402
from llm_wiki.lsp_process_limits import MAX_STARTUP_CLEANUP_OWNERS as MAX_STARTUP_CLEANUP_OWNERS  # noqa: E402
from llm_wiki.lsp_process_limits import MAX_STDERR_BYTES as MAX_STDERR_BYTES  # noqa: E402
from llm_wiki.lsp_process_limits import (
    RECOVERY_RETRY_CEILING_SECONDS as RECOVERY_RETRY_CEILING_SECONDS,  # noqa: E402
)
from llm_wiki.lsp_process_limits import RECOVERY_RETRY_SECONDS as RECOVERY_RETRY_SECONDS  # noqa: E402
from llm_wiki.lsp_process_limits import STARTUP_WAIT_SECONDS as STARTUP_WAIT_SECONDS  # noqa: E402
from llm_wiki.lsp_process_owner import BROAD_ACL_SIDS as BROAD_ACL_SIDS  # noqa: E402
from llm_wiki.lsp_process_owner import ObjectIdentity as ObjectIdentity  # noqa: E402
from llm_wiki.lsp_process_owner import OwnerDirectory as OwnerDirectory  # noqa: E402
from llm_wiki.lsp_process_owner import new_generation_nonce as new_generation_nonce  # noqa: E402
from llm_wiki.lsp_process_owner import require_single_owner_ace as require_single_owner_ace  # noqa: E402
from llm_wiki.lsp_process_owner import secure_windows_owner_root as secure_windows_owner_root  # noqa: E402
from llm_wiki.lsp_process_owner import validated_deadline as validated_deadline  # noqa: E402
from llm_wiki.lsp_process_owner import write_all_descriptor as write_all_descriptor  # noqa: E402
from llm_wiki.lsp_process_recovery import RecoveryState as RecoveryState  # noqa: E402
from llm_wiki.lsp_process_recovery import process_failure_intent as process_failure_intent  # noqa: E402
from llm_wiki.lsp_process_recovery import process_recovery_request as process_recovery_request  # noqa: E402
from llm_wiki.lsp_process_recovery import start_heartbeat_worker as start_heartbeat_worker  # noqa: E402
from llm_wiki.lsp_process_recovery import start_lifecycle_workers as start_lifecycle_workers  # noqa: E402
from llm_wiki.lsp_process_requests import notify_lsp_process as notify_lsp_process  # noqa: E402
from llm_wiki.lsp_process_requests import (
    notify_lsp_process_generation as notify_lsp_process_generation,  # noqa: E402
)
from llm_wiki.lsp_process_requests import (
    promote_lsp_process_workspace_ready as promote_lsp_process_workspace_ready,  # noqa: E402
)
from llm_wiki.lsp_process_requests import request_generation as request_generation  # noqa: E402
from llm_wiki.lsp_process_requests import request_lsp_process as request_lsp_process  # noqa: E402
from llm_wiki.lsp_process_requests import wait_for_generation_change as wait_for_generation_change  # noqa: E402
from llm_wiki.lsp_process_restart import (
    commit_restart_generation_owned as commit_restart_generation_owned,  # noqa: E402
)
from llm_wiki.lsp_process_restart import fail_restart_generation as fail_restart_generation  # noqa: E402
from llm_wiki.lsp_process_restart import (
    prepare_restart_generation_owned as prepare_restart_generation_owned,  # noqa: E402
)
from llm_wiki.lsp_process_restart import restart_generation as restart_generation  # noqa: E402
from llm_wiki.lsp_process_startup import resolved_startup_deadline as resolved_startup_deadline  # noqa: E402
from llm_wiki.lsp_process_startup import (
    start_configured_lsp_process as start_configured_lsp_process,  # noqa: E402
)
from llm_wiki.lsp_process_startup import start_lsp_process as start_lsp_process  # noqa: E402
from llm_wiki.lsp_process_startup import start_lsp_process_impl as start_lsp_process_impl  # noqa: E402
from llm_wiki.lsp_process_state import CleanupError as CleanupError  # noqa: E402
from llm_wiki.lsp_process_state import CleanupResult as CleanupResult  # noqa: E402
from llm_wiki.lsp_process_state import FailureEvidenceIdentity as FailureEvidenceIdentity  # noqa: E402
from llm_wiki.lsp_process_state import FailureIntent as FailureIntent  # noqa: E402
from llm_wiki.lsp_process_state import Generation as Generation  # noqa: E402
from llm_wiki.lsp_process_state import GenerationBootstrap as GenerationBootstrap  # noqa: E402
from llm_wiki.lsp_process_state import GenerationGuardFactory as GenerationGuardFactory  # noqa: E402
from llm_wiki.lsp_process_state import GenerationLaunch as GenerationLaunch  # noqa: E402
from llm_wiki.lsp_process_state import LifecycleCoordinator as LifecycleCoordinator  # noqa: E402
from llm_wiki.lsp_process_state import LifecyclePhase as LifecyclePhase  # noqa: E402
from llm_wiki.lsp_process_state import LspProcessHandle as LspProcessHandle  # noqa: E402
from llm_wiki.lsp_process_state import ProcessState as ProcessState  # noqa: E402
from llm_wiki.lsp_process_state import ProcessTree as ProcessTree  # noqa: E402
from llm_wiki.lsp_process_state import acknowledge_failure_intent as acknowledge_failure_intent  # noqa: E402
from llm_wiki.lsp_process_state import acquire_driver as acquire_driver  # noqa: E402
from llm_wiki.lsp_process_state import acquire_lease as acquire_lease  # noqa: E402
from llm_wiki.lsp_process_state import coordinator_has_ownership as coordinator_has_ownership  # noqa: E402
from llm_wiki.lsp_process_state import drain_stderr as drain_stderr  # noqa: E402
from llm_wiki.lsp_process_state import enqueue_failure_intent as enqueue_failure_intent  # noqa: E402
from llm_wiki.lsp_process_state import generations_locked as generations_locked  # noqa: E402
from llm_wiki.lsp_process_state import lsp_environment as lsp_environment  # noqa: E402
from llm_wiki.lsp_process_state import (
    mark_generation_expected_exit as mark_generation_expected_exit,  # noqa: E402
)
from llm_wiki.lsp_process_state import new_stderr_wake as new_stderr_wake  # noqa: E402
from llm_wiki.lsp_process_state import (
    pending_startup_cleanup_snapshot as pending_startup_cleanup_snapshot,  # noqa: E402
)
from llm_wiki.lsp_process_state import queue_generation_failure as queue_generation_failure  # noqa: E402
from llm_wiki.lsp_process_state import queue_owner_failure as queue_owner_failure  # noqa: E402
from llm_wiki.lsp_process_state import register_startup_cleanup as register_startup_cleanup  # noqa: E402
from llm_wiki.lsp_process_state import release_driver as release_driver  # noqa: E402
from llm_wiki.lsp_process_state import release_lease as release_lease  # noqa: E402
from llm_wiki.lsp_process_state import release_lifecycle as release_lifecycle  # noqa: E402
from llm_wiki.lsp_process_state import (
    remember_background_cleanup_error as remember_background_cleanup_error,  # noqa: E402
)
from llm_wiki.lsp_process_state import subprocess as subprocess  # noqa: E402
from llm_wiki.lsp_process_state import (
    take_background_cleanup_error as take_background_cleanup_error,  # noqa: E402
)
from llm_wiki.lsp_process_state import unconfigured_generation as unconfigured_generation  # noqa: E402
from llm_wiki.lsp_process_state import unregister_startup_cleanup as unregister_startup_cleanup  # noqa: E402
from llm_wiki.lsp_process_state import validate_failure_record as validate_failure_record  # noqa: E402
from llm_wiki.lsp_process_state import write_failure_record as write_failure_record  # noqa: E402
from llm_wiki.lsp_process_state import write_owner_record as write_owner_record  # noqa: E402
from llm_wiki.lsp_process_sweep import process_is_gone as process_is_gone  # noqa: E402
from llm_wiki.lsp_process_sweep import remove_dead_owner_root as remove_dead_owner_root  # noqa: E402
from llm_wiki.lsp_process_sweep import sweep_dead_owner_roots as sweep_dead_owner_roots  # noqa: E402
from lsp_protocol import (
    CancellationToken,
    LspProtocol,
)


@dataclass(slots=True)
class LspProcess(LspProcessHandle):
    process: subprocess.Popen[bytes]
    protocol: LspProtocol
    owner_root: Path
    owner_nonce: str
    generation_nonce: str
    state: ProcessState
    started_monotonic: float
    last_used_monotonic: float
    restart_count: int = 0
    _coordinator: LifecycleCoordinator = field(init=False, repr=False)
    _stderr_projection: deque[bytes] = field(default_factory=deque, init=False, repr=False)
    _stderr_projection_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _command: tuple[str, ...] = field(default=(), init=False, repr=False)
    _cwd: Path | None = field(default=None, init=False, repr=False)
    _environment: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def start(
        cls, command: Sequence[str], *, cwd: Path, owner_root: Path
    ) -> LspProcess:
        return start_lsp_process(cls, command, cwd=cwd, owner_root=owner_root)

    @classmethod
    def start_configured(
        cls,
        command: Sequence[str],
        *,
        cwd: Path,
        owner_root: Path,
        deadline: float,
        server_request_handlers: Mapping[str, Callable[[object], object]],
        server_notification_handlers: Mapping[str, Callable[[object], None]],
        generation_bootstrap: GenerationBootstrap,
        bootstrap_timeout_seconds: float | None = None,
        generation_guard: GenerationGuardFactory | None = None,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> LspProcess:
        return start_configured_lsp_process(
            cls,
            command,
            cwd=cwd,
            owner_root=owner_root,
            deadline=deadline,
            server_request_handlers=server_request_handlers,
            server_notification_handlers=server_notification_handlers,
            generation_bootstrap=generation_bootstrap,
            bootstrap_timeout_seconds=bootstrap_timeout_seconds,
            generation_guard=generation_guard,
            environment_overrides=environment_overrides,
        )

    def request(
        self,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
    ) -> object:
        return request_lsp_process(
            self,
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,
        )

    def notify(self, method: str, params: object, *, deadline: float) -> None:
        """Send once through the active generation without transparent replay."""
        notify_lsp_process(self, method, params, deadline=deadline)

    def notify_generation(
        self,
        method: str,
        params: object,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        """Notify only if the named generation is still active."""
        return notify_lsp_process_generation(
            self,
            method,
            params,
            generation_nonce=generation_nonce,
            deadline=deadline,
        )

    def promote_workspace_ready(
        self,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        """Promote the active generation without racing a terminal transition."""
        return promote_lsp_process_workspace_ready(
            self,
            generation_nonce=generation_nonce,
            deadline=deadline,
        )

    def shutdown(self, deadline: float) -> None:
        shutdown_lsp_process(self, deadline)

    def cancel_all(self, reason: str) -> None:
        cancel_all_lsp_process(self, reason)

    def restart(self, deadline: float) -> None:
        restart_lsp_process(self, deadline)

    def close(self, deadline: float) -> None:
        shutdown_lsp_process(self, deadline)

    def wait_for_exit(self, deadline: float) -> int:
        return wait_for_lsp_exit(self, deadline)

    def _terminal_failure(self, code: str, deadline: float) -> None:
        lsp_process_cleanup.terminal_failure_lsp_process(self, code, deadline)

    def _atexit_close(self) -> None:
        try:
            self.close(time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS)
        except BaseException:
            pass

    def _stop_heartbeat(self, deadline: float) -> None:
        try:
            lsp_process_failure.stop_heartbeat_owner(self._coordinator, validated_deadline(deadline))
        except TimeoutError as exc:
            raise TimeoutError("LSP heartbeat thread did not stop before deadline") from exc
