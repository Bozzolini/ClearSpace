from __future__ import annotations

import os
from functools import lru_cache
from typing import Iterable, Optional, Sequence

CRITICAL_SYSTEM_FILENAMES: frozenset[str] = frozenset(
    {
        "pagefile.sys",
        "hiberfil.sys",
        "swapfile.sys",
        "bootmgr",
        "bootnxt",
        "ntldr",
        "io.sys",
        "msdos.sys",
        "desktop.ini",
        "ntuser.dat",
        "ntuser.dat.log1",
        "ntuser.dat.log2",
        "usrclass.dat",
    }
)

PROTECTED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        "$windows.~bt",
        "$windows.~ws",
        "$winreagent",
        "config.msi",
        "found.000",
    }
)


# Encryption keys, credentials and certificates: often years old (so they look like "old files")
# and irreplaceable — losing the DPAPI master keys makes every saved password unreadable.
_SECRET_MS_DIRS: tuple[str, ...] = (
    "Protect",
    "Credentials",
    "Crypto",
    "SystemCertificates",
    "Vault",
)
_SECRET_HOME_DIRS: tuple[str, ...] = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker")

# Credential stores, private keys, password DBs, crypto wallets: never scan or clean.
SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        "login data",
        "login data-journal",
        "cookies",
        "cookies-journal",
        "web data",
        "web data-journal",
        "logins.json",
        "key3.db",
        "key4.db",
        "cert8.db",
        "cert9.db",
        "signons.sqlite",
        "wallet.dat",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "identity",
        "masterkey",
        "unattend.xml",
        "autounattend.xml",
        ".env",
        "credentials",
        "credential",
        "secrets.json",
        "serviceaccount.json",
    }
)
SENSITIVE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pem",
        ".pfx",
        ".p12",
        ".p7b",
        ".key",
        ".kdbx",
        ".kdb",
        ".asc",
        ".ppk",
        ".psafe3",
    }
)
SENSITIVE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".password-store",
        "electrum",
        "wallets",
    }
)


# Folders Windows / Explorer expect to exist; removing them even when empty breaks the shell.
USER_SHELL_FOLDERS: frozenset[str] = frozenset(
    {
        "appdata",
        "desktop",
        "documents",
        "downloads",
        "music",
        "pictures",
        "videos",
        "favorites",
        "contacts",
        "links",
        "saved games",
        "searches",
        "onedrive",
        "3d objects",
        "templates",
        "libraries",
    }
)


def normalize_win_path(path: str) -> str:
    """Normalize a Windows path for fast boundary-safe comparison without syscalls."""
    if not path:
        return ""
    p = path
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    p = os.path.normcase(os.path.normpath(p))
    # Preserve drive root like 'c:\' while stripping trailing slash on subdirs
    if len(p) > 3:
        p = p.rstrip("\\/")
    return p


def default_exclusions(extra: Optional[Iterable[str]] = None) -> list[str]:
    """Return critical Windows system paths that must never be scanned or cleaned."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")

    paths: list[str] = [
        windir,
        program_files,
        program_files_x86,
        program_data,
        rf"{system_drive}\$Recycle.Bin",
        rf"{system_drive}\System Volume Information",
        rf"{system_drive}\Recovery",
        rf"{system_drive}\Boot",
        rf"{system_drive}\PerfLogs",
        rf"{system_drive}\MSOCache",
    ]
    if local_appdata:
        paths.append(os.path.join(local_appdata, "Programs"))
        paths.append(os.path.join(local_appdata, "Microsoft", "WindowsApps"))
        paths.extend(os.path.join(local_appdata, "Microsoft", d) for d in _SECRET_MS_DIRS)
    if appdata:
        paths.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu"))
        paths.extend(os.path.join(appdata, "Microsoft", d) for d in _SECRET_MS_DIRS)
        paths.append(os.path.join(appdata, "gnupg"))
    if user_profile:
        paths.extend(os.path.join(user_profile, d) for d in _SECRET_HOME_DIRS)

    if extra:
        paths.extend(p for p in extra if p)

    seen: set[str] = set()
    result: list[str] = []
    for p in paths:
        if not p:
            continue
        norm = normalize_win_path(p)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(os.path.normpath(p))
    return result


def build_exclusion_prefixes(paths: Optional[Sequence[str]] = None) -> tuple[str, ...]:
    """Precompute normalized prefixes for fast boundary-safe checks."""
    raw = paths if paths is not None else default_exclusions()
    out: list[str] = []
    seen: set[str] = set()
    for p in raw:
        norm = normalize_win_path(p)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    out.sort(key=len, reverse=True)
    return tuple(out)


def protected_prefixes(custom_exclusions: Optional[Iterable[str]] = None) -> tuple[str, ...]:
    """Built-in system exclusions plus user exclusions. Custom entries never replace the defaults."""
    return build_exclusion_prefixes(default_exclusions(custom_exclusions))


@lru_cache(maxsize=64)
def _compiled_prefixes(prefixes: tuple[str, ...]) -> tuple[frozenset[str], tuple[str, ...]]:
    exact: set[str] = set()
    bounded: list[str] = []
    for pref in prefixes:
        if not pref:
            continue
        np = normalize_win_path(pref)
        exact.add(np)
        bounded.append(np if np.endswith("\\") else np + "\\")
    return frozenset(exact), tuple(bounded)


def is_sensitive_norm(norm: str) -> bool:
    """True for private keys, password DBs, wallets, credential stores."""
    if not norm:
        return False
    parts = norm.split("\\")
    name = parts[-1] if parts else ""
    if name in SENSITIVE_FILENAMES or name in CRITICAL_SYSTEM_FILENAMES:
        return True
    _, ext = os.path.splitext(name)
    if ext in SENSITIVE_EXTENSIONS:
        return True
    return not SENSITIVE_DIR_NAMES.isdisjoint(parts)


def is_under_excluded_norm(norm: str, prefixes: Sequence[str]) -> bool:
    """Same as `is_under_excluded` for a path already passed through `normalize_win_path`."""
    if not norm:
        return False
    parts = norm.split("\\")
    # Protected directory names on any drive (e.g. D:\$Recycle.Bin, E:\System Volume Information)
    if not PROTECTED_DIR_NAMES.isdisjoint(parts[1:]):
        return True
    if parts[-1] in CRITICAL_SYSTEM_FILENAMES:
        return True
    if is_sensitive_norm(norm):
        return True
    exact, bounded = _compiled_prefixes(tuple(prefixes))
    return norm in exact or (bool(bounded) and norm.startswith(bounded))


def is_structural_folder_norm(norm: str) -> bool:
    """Drive roots, X:\\Users, each profile root and the known folders inside it."""
    parts = [p for p in norm.split("\\") if p]
    if len(parts) <= 1:
        return True
    if parts[1] != "users":
        return False
    return len(parts) <= 3 or (len(parts) == 4 and parts[3] in USER_SHELL_FOLDERS)


def is_under_excluded(path: str, prefixes: Sequence[str]) -> bool:
    """Fast boundary-checked exclusion test. Never matches partial folder names."""
    if not path:
        return False
    return is_under_excluded_norm(normalize_win_path(path), prefixes)
