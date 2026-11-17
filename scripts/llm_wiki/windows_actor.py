"""Windows process-token SID lookup; independent of the ownership registry."""
from __future__ import annotations

import ctypes
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowsTokenApi:
    get_current_process: object
    open_process_token: object
    get_token_information: object
    convert_sid: object
    local_free: object
    close_handle: object


def windows_token_api(advapi32: object, kernel32: object) -> WindowsTokenApi:
    """Bind the token entry points this identity uses, with exact signatures."""
    from ctypes import wintypes

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_token_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    convert_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return WindowsTokenApi(
        get_current_process=get_current_process,
        open_process_token=open_process_token,
        get_token_information=get_token_information,
        convert_sid=convert_sid,
        local_free=local_free,
        close_handle=close_handle,
    )


def windows_open_token(api: WindowsTokenApi) -> object:
    from ctypes import wintypes

    token = wintypes.HANDLE()
    if not api.open_process_token(
        api.get_current_process(), 0x0008, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return token


def windows_token_information(api: WindowsTokenApi, token: object) -> object:
    from ctypes import wintypes

    required = wintypes.DWORD()
    api.get_token_information(token, 1, None, 0, ctypes.byref(required))
    if ctypes.get_last_error() != 122 or required.value <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(required.value)
    if not api.get_token_information(
        token, 1, buffer, required.value, ctypes.byref(required)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer


def windows_token_sid_text(api: WindowsTokenApi, token: object) -> str | None:
    from ctypes import wintypes

    buffer = windows_token_information(api, token)
    sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    sid_text = wintypes.LPWSTR()
    if not api.convert_sid(sid, ctypes.byref(sid_text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return sid_text.value
    finally:
        api.local_free(sid_text)


def validated_windows_actor(value: object) -> str:
    if value is None or re.fullmatch(r"S-[0-9]+(?:-[0-9]+)+", value) is None:
        raise OSError("Windows actor SID was malformed")
    actor = f"windows-sid:{value}"
    if len(actor.encode("ascii")) > 256:
        raise OSError("Windows actor SID exceeded the ownership bound")
    return actor


def windows_actor_identity() -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    api = windows_token_api(advapi32, kernel32)
    token = windows_open_token(api)
    try:
        value = windows_token_sid_text(api, token)
    finally:
        api.close_handle(token)
    return validated_windows_actor(value)

# Preserve historical serialized identities through the old aliases.
WindowsTokenApi.__module__ = "operational_ownership"
windows_token_api.__module__ = "operational_ownership"
windows_open_token.__module__ = "operational_ownership"
windows_token_information.__module__ = "operational_ownership"
windows_token_sid_text.__module__ = "operational_ownership"
validated_windows_actor.__module__ = "operational_ownership"
windows_actor_identity.__module__ = "operational_ownership"
