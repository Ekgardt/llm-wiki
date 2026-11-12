"""Launch a managed language server from a sealed, digest-verified copy of its entry point."""

import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

import lsp_launch_package
import windows_workspace as _windows_workspace
from interruption import raise_collected_errors as _raise_collected_errors
from llm_wiki.pyright_session_paths import path_identity, require_startup_deadline
from llm_wiki.pyright_session_values import BootstrapDegradation
from lsp_process import GenerationLaunch
from pyright_profile import MAX_SERVER_BYTES

_WINDOWS_STAT_CREATION_TIME = os.name == "nt"


_NODE_MAIN_LOADER = "\n".join(
    (
        '"use strict";',
        'const fs = require("node:fs");',
        'const path = require("node:path");',
        'const Module = require("node:module");',
        "const descriptorText = process.argv[1];",
        "const filename = process.argv[2];",
        'if (!/^(0|[1-9][0-9]*)$/.test(descriptorText)) throw new Error("invalid held descriptor");',
        "const descriptor = Number(descriptorText);",
        'if (!Number.isSafeInteger(descriptor)) throw new Error("invalid held descriptor");',
        'const source = fs.readFileSync(descriptor, "utf8");',
        "fs.closeSync(descriptor);",
        "const argv = process.argv.slice(3);",
        "process.argv = [process.execPath, filename, ...argv];",
        "const main = new Module(filename, null);",
        'main.id = ".";',
        "main.path = path.dirname(filename);",
        "main.filename = filename;",
        "main.paths = Module._nodeModulePaths(main.path);",
        "process.mainModule = main;",
        "Module._cache[filename] = main;",
        "main._compile(source, filename);",
        "main.loaded = true;",
    )
)


def _launch_file_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        0 if _WINDOWS_STAT_CREATION_TIME else info.st_ctime_ns,
    )


def released_error(release: Callable[[], object]) -> BaseException | None:
    """Run one release step; the error it raised, if it raised one."""
    try:
        release()
    except BaseException as error:
        return error
    return None


def _unlink_error(path: Path | None) -> BaseException | None:
    """Remove a file that may already be gone; the error worth reporting."""
    if path is None:
        return None
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except BaseException as error:
        return error
    return None


def _closed_descriptor_error(descriptor: int | None) -> BaseException | None:
    if descriptor is None:
        return None
    return released_error(lambda: os.close(descriptor))


class LaunchServerGuard:
    def __init__(
        self,
        path: Path,
        expected_sha256: str,
        *,
        command: tuple[str, ...],
        owner_root: Path,
        deadline: float,
        degradation_prefix: str = "pyright",
        package_launch: object | None = None,
        native: bool = False,
    ) -> None:
        if not isinstance(owner_root, Path):
            raise TypeError("owner_root must be a Path")
        self._degradation_prefix = degradation_prefix
        self._package_launch = package_launch
        self._native = native
        self._launch_tree: object | None = None
        self._path = path
        self._expected_sha256 = expected_sha256
        self._command = command
        self._owner_root = owner_root
        self._deadline = deadline
        self._descriptor: int | None = None
        self._snapshot: BinaryIO | None = None
        self._snapshot_path: Path | None = None
        self._launch_descriptor: int | None = None
        self._state: tuple[int, int, int, int, int, int] | None = None

    def _digest_mismatch(self) -> BootstrapDegradation:
        """One refusal, named for whichever server this guard is launching."""
        return BootstrapDegradation(
            f"{self._degradation_prefix}_executable_digest_mismatch"
        )

    def _open_source_descriptor(self) -> int:
        """Open the server file exclusively, the way this platform allows."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if os.name != "nt":
            return os.open(
                self._path,
                flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        import msvcrt

        handle = _windows_workspace.open_exclusive_readonly_source_file(self._path)
        try:
            return msvcrt.open_osfhandle(handle, flags)
        except BaseException:
            _windows_workspace.close_handle(handle)
            raise

    def _check_unchanged(self, before: os.stat_result, descriptor: int) -> None:
        """What we opened has to be the same regular file we just measured."""
        opened = os.fstat(descriptor)
        state = _launch_file_state(opened)
        if _launch_file_state(before) != state or not stat.S_ISREG(opened.st_mode):
            raise self._digest_mismatch()
        self._state = state

    def _make_executable(self, path: Path | None) -> None:
        """A native server has to stay executable in its verified copy.

        `mkstemp` creates the copy at mode 600, which is right for a file Node
        will read and wrong for a file the kernel has to execute.
        """
        if not self._native or path is None:
            return
        path.chmod(path.stat().st_mode | 0o500)

    def _open_snapshot(self) -> BinaryIO:
        """A private copy of the server, under our own owner root."""
        snapshot_descriptor, snapshot_name = tempfile.mkstemp(
            prefix=".launch-",
            suffix=".tmp",
            dir=self._owner_root,
        )
        self._snapshot_path = Path(snapshot_name)
        try:
            snapshot = os.fdopen(snapshot_descriptor, "w+b")
        except BaseException:
            os.close(snapshot_descriptor)
            raise
        self._snapshot = snapshot
        return snapshot

    def _check_snapshot_launchable(
        self,
        launch_info: os.stat_result,
        snapshot_info: os.stat_result,
        before: os.stat_result,
    ) -> None:
        """The descriptor we will launch has to name our verified copy."""
        launchable = (
            stat.S_ISREG(launch_info.st_mode)
            and launch_info.st_size == before.st_size
            and _launch_file_state(launch_info) == _launch_file_state(snapshot_info)
        )
        if not launchable:
            raise self._digest_mismatch()

    def _checkpoint(self) -> None:
        require_startup_deadline(self._deadline)

    def _posix_launch(self, before: os.stat_result) -> GenerationLaunch:
        """Copy the server aside, verify it, and launch from the copy."""
        snapshot = self._open_snapshot()
        self._verify_digest(self._copy_snapshot(snapshot))
        self._make_executable(self._snapshot_path)
        if self._native:
            return self._native_path_launch(snapshot)
        if self._package_launch is not None:
            return self._package_launch_from(snapshot)
        return self._descriptor_launch(snapshot, before)

    def _native_path_launch(self, snapshot: BinaryIO) -> GenerationLaunch:
        """Launch the verified copy by its path, inside our own owner root.

        A native server reads its own executable while it runs -- gopls hashes
        it for its index and re-executes it for telemetry -- so the copy has to
        keep a name. The owner root is created for this process tree alone, and
        the digest was verified on the bytes that were written there. Research:
        `docs/research/2026-09-12-installing-go-and-building-gopls.md`.

        The guard therefore stops owning the file: until 2026-09-17 it kept the
        path and unlinked it on the way out of the `with` block, taking the name
        away from the server that had just been started by it. What removes it
        now is what removes everything else in the owner root, when the
        generation ends.
        """
        path = self._snapshot_path
        if path is None:
            raise RuntimeError("native launch has no verified copy")
        snapshot.close()
        self._snapshot = None
        self._snapshot_path = None
        return GenerationLaunch((str(path), *self._command[1:]), ())

    def _package_launch_from(self, snapshot: BinaryIO) -> GenerationLaunch:
        """Seal the verified copy into a package root and launch that path.

        The descriptor launch below cannot execute a server whose entry reads a
        file relative to its own location; see `scripts/lsp_launch_package.py`.
        """
        tree = self._sealed_launch_tree(snapshot)
        self._launch_tree = tree
        self._snapshot_path = None
        snapshot.close()
        self._snapshot = None
        return GenerationLaunch(
            (self._command[0], str(tree.entry), *self._command[2:]), ()
        )

    def _sealed_launch_tree(self, snapshot: BinaryIO) -> lsp_launch_package.LaunchTree:
        snapshot_path = self._snapshot_path
        if snapshot_path is None:
            raise RuntimeError("launch server guard is closed")
        try:
            return lsp_launch_package.create_launch_tree(
                self._owner_root,
                self._package_launch,
                snapshot_path=snapshot_path,
                snapshot_descriptor=snapshot.fileno(),
                expected_sha256=self._expected_sha256,
                checkpoint=self._checkpoint,
            )
        except lsp_launch_package.LaunchTreeError as error:
            raise self._digest_mismatch() from error

    def _descriptor_launch(
        self, snapshot: BinaryIO, before: os.stat_result
    ) -> GenerationLaunch:
        """Unlink the verified copy and hand `node` the descriptor instead."""
        launch_descriptor = os.open(
            self._snapshot_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        self._launch_descriptor = launch_descriptor
        self._check_snapshot_launchable(
            os.fstat(launch_descriptor), os.fstat(snapshot.fileno()), before
        )
        os.unlink(self._snapshot_path)
        self._snapshot_path = None
        snapshot.close()
        self._snapshot = None
        return GenerationLaunch(
            self._posix_launch_command(launch_descriptor),
            (launch_descriptor,),
        )

    def __enter__(self) -> LaunchServerGuard | GenerationLaunch:
        require_startup_deadline(self._deadline)
        before = path_identity(self._path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SERVER_BYTES:
            raise self._digest_mismatch()
        require_startup_deadline(self._deadline)
        self._descriptor = self._open_source_descriptor()
        try:
            require_startup_deadline(self._deadline)
            self._check_unchanged(before, self._descriptor)
            if os.name == "posix":
                return self._posix_launch(before)
            self.verify()
        except BaseException:
            self.close()
            raise
        return self

    @staticmethod
    def _descriptor_path(descriptor: int) -> str:
        for root in (Path("/proc/self/fd"), Path("/dev/fd")):
            if root.is_dir():
                return str(root / str(descriptor))
        raise RuntimeError("POSIX inherited descriptor paths are unavailable")

    def _posix_launch_command(self, descriptor: int) -> tuple[str, ...]:
        if self._native:
            return self._native_launch_command(descriptor)
        if self._path.suffix.casefold() == ".js":
            return (
                self._command[0],
                "--eval",
                _NODE_MAIN_LOADER,
                "--",
                str(descriptor),
                str(self._path),
                *self._command[2:],
            )
        return (
            self._command[0],
            self._descriptor_path(descriptor),
            *self._command[2:],
        )

    def _native_launch_command(self, descriptor: int) -> tuple[str, ...]:
        """A native server is executed *as* the verified descriptor.

        Handing the descriptor path as an argument is what an interpreter
        wants; a native executable reads it as its own first argument --
        `gopls /proc/self/fd/7` is an unknown subcommand, and the process
        exits before the handshake. The descriptor is the program here, so it
        takes argv[0]'s place and the profile's own flags follow. Research:
        `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
        """
        return (self._descriptor_path(descriptor), *self._command[1:])

    def _rewound_descriptor(self) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise RuntimeError("Pyright launch server guard is closed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor

    def _server_chunks(self, descriptor: int) -> Iterator[bytes]:
        """The server file in bounded chunks, each read inside the startup deadline."""
        total = 0
        while True:
            require_startup_deadline(self._deadline)
            chunk = os.read(descriptor, 64 * 1024)
            require_startup_deadline(self._deadline)
            if not chunk:
                return
            total += len(chunk)
            if total > MAX_SERVER_BYTES:
                raise self._digest_mismatch()
            yield chunk

    def _copy_snapshot(self, snapshot: BinaryIO) -> str:
        descriptor = self._rewound_descriptor()
        snapshot.seek(0)
        digest = hashlib.sha256()
        for chunk in self._server_chunks(descriptor):
            snapshot.write(chunk)
            digest.update(chunk)
        snapshot.flush()
        snapshot.seek(0)
        return digest.hexdigest()

    def _digest(self) -> str:
        descriptor = self._rewound_descriptor()
        digest = hashlib.sha256()
        for chunk in self._server_chunks(descriptor):
            digest.update(chunk)
        return digest.hexdigest()

    def verify(self) -> None:
        require_startup_deadline(self._deadline)
        descriptor = self._descriptor
        state = self._state
        if descriptor is None or state is None:
            raise RuntimeError("Pyright launch server guard is not open")
        if _launch_file_state(os.fstat(descriptor)) != state:
            raise self._digest_mismatch()
        actual = self._digest()
        self._verify_digest(actual)

    def _verify_digest(self, actual: str) -> None:
        descriptor = self._descriptor
        state = self._state
        if descriptor is None or state is None:
            raise RuntimeError("launch server guard is not open")
        require_startup_deadline(self._deadline)
        if actual != self._expected_sha256:
            raise self._digest_mismatch()
        self._require_unchanged_state(descriptor, state)
        require_startup_deadline(self._deadline)

    def _require_unchanged_state(self, descriptor: int, state: tuple) -> None:
        """The open descriptor and the path still name the file we digested."""
        observed = (
            _launch_file_state(os.fstat(descriptor)),
            _launch_file_state(path_identity(self._path)),
        )
        if observed != (state, state):
            raise self._digest_mismatch()

    def _take_owned_resources(
        self,
    ) -> tuple[object, Path | None, int | None, int | None, object]:
        """Hand over everything the guard holds, leaving it holding nothing."""
        snapshot, self._snapshot = self._snapshot, None
        snapshot_path, self._snapshot_path = self._snapshot_path, None
        launch_descriptor, self._launch_descriptor = self._launch_descriptor, None
        descriptor, self._descriptor = self._descriptor, None
        tree, self._launch_tree = self._launch_tree, None
        return snapshot, snapshot_path, launch_descriptor, descriptor, tree

    def close(self) -> None:
        snapshot, snapshot_path, launch_descriptor, descriptor, tree = (
            self._take_owned_resources()
        )
        errors = [
            released_error(snapshot.close) if snapshot is not None else None,
            _unlink_error(snapshot_path),
            _closed_descriptor_error(launch_descriptor),
            _closed_descriptor_error(descriptor),
            lsp_launch_package.remove_launch_tree(tree),
        ]
        _raise_collected_errors([item for item in errors if item is not None])

    def _verified_operation_error(
        self, error_type: object, error_info: tuple[object, ...]
    ) -> BaseException | None:
        """The error the block carried, or the one verification found."""
        for item in error_info:
            if isinstance(item, BaseException):
                return item
        if error_type is not None:
            return None
        try:
            self.verify()
        except BaseException as error:
            return error
        return None

    def __exit__(self, error_type: object, *error_info: object) -> None:
        operation_error = self._verified_operation_error(error_type, error_info)
        try:
            self.close()
        except BaseException as cleanup_error:
            if operation_error is not None:
                _raise_collected_errors(
                    [cleanup_error],
                    prior_error=operation_error,
                )
            raise
        if error_type is None and operation_error is not None:
            raise operation_error.with_traceback(operation_error.__traceback__)
