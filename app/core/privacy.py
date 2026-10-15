"""Local-only privacy helpers: no network, owner-only ACLs, log/cache erasure."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.core.logging_config import close_log_files, get_app_data_dir, get_log_dir

_SYSTEM_SID = "S-1-5-18"
_LOCKED_PATHS: set[str] = set()


def windows_dir() -> str:
    return os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"


def explorer_exe() -> str:
    """Absolute explorer.exe — never a PATH-hijackable bare 'explorer'."""
    return os.path.join(windows_dir(), "explorer.exe")


def system32(name: str) -> str:
    return os.path.join(windows_dir(), "System32", name)


def _current_user_sid() -> str:
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        adv = ctypes.windll.advapi32
        kernel = ctypes.windll.kernel32
        token = wintypes.HANDLE()
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return ""

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", SID_AND_ATTRIBUTES)]

        try:
            needed = wintypes.DWORD(0)
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value)
            if not adv.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
                return ""
            user = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
            sid_str = ctypes.c_wchar_p()
            if not adv.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(sid_str)):
                return ""
            try:
                return sid_str.value or ""
            finally:
                kernel.LocalFree(sid_str)
        finally:
            kernel.CloseHandle(token)
    except Exception:  # noqa: BLE001
        return ""


def restrict_to_current_user(path: str | Path) -> None:
    """Replace inherited ACLs so only this user and SYSTEM can read/write `path`.

    Other local accounts (and the Users group) lose access. Administrators can still
    take ownership; that is a Windows rule, not something an app can revoke.
    """
    target = os.fspath(path)
    if not target or not os.path.exists(target):
        return
    if os.name != "nt":
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        return
    if target in _LOCKED_PATHS:
        return
    icacls = system32("icacls.exe")
    if not os.path.isfile(icacls):
        return
    sid = _current_user_sid()
    if not sid:
        return
    rights = "(OI)(CI)F" if os.path.isdir(target) else "F"
    try:
        subprocess.run(  # noqa: S603
            [
                icacls,
                target,
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:{rights}",
                "/grant:r",
                f"*{_SYSTEM_SID}:{rights}",
            ],
            check=False,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _LOCKED_PATHS.add(target)
    except OSError:
        pass


def lock_app_data_dir() -> None:
    restrict_to_current_user(get_app_data_dir())


def wipe_logs() -> None:
    close_log_files()
    log_dir = get_log_dir()
    if not log_dir.is_dir():
        return
    for p in log_dir.glob("clearspace.log*"):
        try:
            p.unlink()
        except OSError:
            pass


def erase_local_data() -> None:
    """Delete the on-disk file index, scan history, results cache and log files.

    Does not delete config.json (language/theme/thresholds). The Recycle Bin of
    the user's own files is not touched.
    """
    from app.core.cache import wipe_all_data

    wipe_all_data()
    wipe_logs()


def open_in_explorer(path: str) -> None:
    """Show `path` in Explorer without going through PATH or cmd.exe."""
    target = os.path.normpath(path)
    exe = explorer_exe()
    if os.path.exists(target) and os.path.isfile(exe):
        subprocess.Popen(  # noqa: S603
            [exe, "/select,", target],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    parent = os.path.dirname(target)
    if os.path.isdir(parent):
        os.startfile(parent)  # type: ignore[attr-defined]  # noqa: S606


def basename_for_log(path: str) -> str:
    """Log-safe identity: filename only, never a home-directory path."""
    name = os.path.basename(path.rstrip("\\/"))
    return name or "<path>"
