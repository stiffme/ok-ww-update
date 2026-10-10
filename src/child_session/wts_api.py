"""Thin ctypes wrapper around the undocumented "child session" APIs of wtsapi32.dll.

Ported from BetterGenshinImpact `Service/ChildSession/ChildSessionNativeMethods.cs`.
The child session is a second Windows Terminal-Services session rendered over a
loopback RDP connection; it is what makes the desktop clone (second game client)
possible. Enabling it requires administrator rights and a Windows edition that
supports sessions (10/11 Pro/Enterprise/Education; Home does not).

`WTSEnableChildSessions`, `WTSIsChildSessionsEnabled` and `WTSGetChildSessionId`
are exported by wtsapi32.dll but are not part of the public SDK headers, so they
are resolved dynamically and may be missing on some builds.
"""

import ctypes
import os
import sys
from ctypes import wintypes

from ok import Logger

logger = Logger.get_logger(__name__)

IS_WINDOWS = sys.platform == "win32"

# WTSGetChildSessionId returns this when the current session has no child.
NO_CHILD_SESSION_ID = 0xFFFFFFFF
# Win32 ERROR_NOT_FOUND, returned by WTSGetChildSessionId when unsupported/absent.
ERROR_NOT_FOUND = 1168
DEFAULT_RDP_PORT = 3389

_RDP_TCP_KEY = r"SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp"
_TERM_SERVICE_PARAMS_KEY = r"SYSTEM\CurrentControlSet\Services\TermService\Parameters"
_RDPWRAP_LIBRARY = "rdpwrap.dll"

# 游戏客户端与启动器进程名（与 config.py `windows.exe` 及注册表启动器名一致），
# 用于强制终止时识别 Child Session 里运行的游戏。
_GAME_PROCESS_NAMES = ("client-win64-shipping.exe", "wuthering waves.exe")

_PROCESS_TERMINATE = 0x0001


class _WTS_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("ProcessId", wintypes.DWORD),
        ("pProcessName", ctypes.c_char_p),
        ("pUserSid", ctypes.c_void_p),
    ]


class ChildSessionError(RuntimeError):
    """Raised when a child-session Win32 call fails."""


if IS_WINDOWS:
    _wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

    _WTSEnableChildSessions = getattr(_wtsapi32, "WTSEnableChildSessions", None)
    _WTSIsChildSessionsEnabled = getattr(_wtsapi32, "WTSIsChildSessionsEnabled", None)
    _WTSGetChildSessionId = getattr(_wtsapi32, "WTSGetChildSessionId", None)
    _WTSLogoffSession = getattr(_wtsapi32, "WTSLogoffSession", None)
    _WTSEnumerateProcesses = getattr(_wtsapi32, "WTSEnumerateProcessesA", None)
    _WTSFreeMemory = getattr(_wtsapi32, "WTSFreeMemory", None)

    if _WTSEnableChildSessions is not None:
        _WTSEnableChildSessions.argtypes = [wintypes.BOOL]
        _WTSEnableChildSessions.restype = wintypes.BOOL
    if _WTSIsChildSessionsEnabled is not None:
        _WTSIsChildSessionsEnabled.argtypes = [ctypes.POINTER(wintypes.BOOL)]
        _WTSIsChildSessionsEnabled.restype = wintypes.BOOL
    if _WTSGetChildSessionId is not None:
        _WTSGetChildSessionId.argtypes = [ctypes.POINTER(wintypes.ULONG)]
        _WTSGetChildSessionId.restype = wintypes.BOOL
    if _WTSLogoffSession is not None:
        _WTSLogoffSession.argtypes = [wintypes.HANDLE, wintypes.ULONG, wintypes.BOOL]
        _WTSLogoffSession.restype = wintypes.BOOL
    if _WTSEnumerateProcesses is not None:
        _WTSEnumerateProcesses.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_WTS_PROCESS_INFO)),
            ctypes.POINTER(wintypes.DWORD),
        ]
        _WTSEnumerateProcesses.restype = wintypes.BOOL
    if _WTSFreeMemory is not None:
        _WTSFreeMemory.argtypes = [ctypes.c_void_p]
        _WTSFreeMemory.restype = None
else:  # pragma: no cover - non-Windows dev only
    _WTSEnableChildSessions = None
    _WTSIsChildSessionsEnabled = None
    _WTSGetChildSessionId = None
    _WTSLogoffSession = None
    _WTSEnumerateProcesses = None
    _WTSFreeMemory = None


def is_supported() -> bool:
    """True if the wtsapi32 child-session entry points are present."""
    return (
        _WTSEnableChildSessions is not None
        and _WTSIsChildSessionsEnabled is not None
        and _WTSGetChildSessionId is not None
    )


def _raise_last_error(operation):
    error = ctypes.get_last_error()
    raise ChildSessionError(f"{operation}（Win32 错误 {error}）")


def enable_child_sessions():
    """Enable child sessions for the current session. Requires admin."""
    if not is_supported():
        raise ChildSessionError("当前系统不支持桌面分身（缺少 WTS Child Session API）")
    if not _WTSEnableChildSessions(True):
        _raise_last_error("无法启用 RDP Child Session")


def is_child_sessions_enabled() -> bool:
    enabled = wintypes.BOOL()
    if not is_supported() or not _WTSIsChildSessionsEnabled(ctypes.byref(enabled)):
        _raise_last_error("无法读取 RDP Child Session 状态")
    return bool(enabled.value)


def get_child_session_id():
    """Return the child session id, or None when there is no child session."""
    if not is_supported():
        raise ChildSessionError("当前系统不支持桌面分身（缺少 WTS Child Session API）")
    session_id = wintypes.ULONG()
    if not _WTSGetChildSessionId(ctypes.byref(session_id)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise ChildSessionError(f"无法取得 RDP Child Session ID（Win32 错误 {error}）")
    if session_id.value == NO_CHILD_SESSION_ID:
        return None
    return session_id.value


def logoff_child_session(session_id, wait=True):
    """Log off the given child session. Returns the session id."""
    if _WTSLogoffSession is None:
        raise ChildSessionError("当前系统不支持桌面分身（缺少 WTSLogoffSession）")
    if not _WTSLogoffSession(None, session_id, wait):
        _raise_last_error(f"无法注销 Child Session {session_id}")
    return session_id


def terminate_child_session(wait=True):
    """Log off the current child session if any. Returns the id or None."""
    session_id = get_child_session_id()
    if session_id is None:
        return None
    return logoff_child_session(session_id, wait=wait)


def _child_okww_process_name():
    """Lowercased exe name of this app's child instance (frozen exe or python)."""
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable).lower()
    return "python.exe"


def _processes_in_session(session_id):
    """Return {pid: lowercase_name} for every process running in `session_id`."""
    if _WTSEnumerateProcesses is None:
        raise ChildSessionError("当前系统不支持枚举 Child Session 进程")
    infos = ctypes.POINTER(_WTS_PROCESS_INFO)()
    count = wintypes.DWORD()
    if not _WTSEnumerateProcesses(None, 0, 1, ctypes.byref(infos), ctypes.byref(count)):
        _raise_last_error("无法枚举 Child Session 进程")
    result = {}
    try:
        for i in range(count.value):
            info = infos[i]
            if info.SessionId == session_id:
                name = (info.pProcessName or b"").decode("ascii", "ignore").lower()
                result[info.ProcessId] = name
    finally:
        if _WTSFreeMemory is not None:
            _WTSFreeMemory(infos)
    return result


def terminate_process(pid):
    """Force-terminate `pid`. Returns True on success, False otherwise (non-fatal)."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 0xFFFFFFFF))
    finally:
        kernel32.CloseHandle(handle)


def kill_child_session():
    """Force-terminate the game + child ok-ww in the child session, then log off.

    Returns the session id that was torn down, or None if there was no child
    session. Logging off terminates anything left over, so this is a full teardown
    of the desktop clone and everything running inside it.
    """
    session_id = get_child_session_id()
    if session_id is None:
        return None

    killable = set(_GAME_PROCESS_NAMES)
    killable.add(_child_okww_process_name())
    try:
        processes = _processes_in_session(session_id)
    except Exception as ex:
        logger.warning(f"枚举 Child Session 进程失败（仍会注销会话）：{ex}")
        processes = {}
    for pid, name in processes.items():
        if name in killable:
            if terminate_process(pid):
                logger.info(f"已终止 Child Session 进程：{name} (pid={pid})")
            else:
                logger.warning(f"无法终止 Child Session 进程：{name} (pid={pid})")

    logoff_child_session(session_id, wait=True)
    return session_id


def get_rdp_port() -> int:
    """Read the configured RDP port from the registry (default 3389)."""
    if not IS_WINDOWS:
        return DEFAULT_RDP_PORT
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _RDP_TCP_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            port, _ = winreg.QueryValueEx(key, "PortNumber")
            if isinstance(port, int) and 0 < port <= 0xFFFF:
                return port
    except OSError:
        pass
    return DEFAULT_RDP_PORT


def is_rdp_wrapper_enabled() -> bool:
    """True when TermService is hooked by rdpwrap.dll (diagnostics only)."""
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _TERM_SERVICE_PARAMS_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            service_dll, _ = winreg.QueryValueEx(key, "ServiceDll")
            return isinstance(service_dll, str) and _RDPWRAP_LIBRARY in service_dll.lower()
    except OSError:
        return False


if __name__ == "__main__":
    # Admin-only smoke test: python -m src.child_session.wts_api
    print("supported:", is_supported())
    if is_supported():
        print("enabled  :", is_child_sessions_enabled())
        print("child id :", get_child_session_id())
    print("rdp port :", get_rdp_port())
    print("rdpwrap  :", is_rdp_wrapper_enabled())
