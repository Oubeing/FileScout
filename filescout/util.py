"""Shared helpers: formatting, size/date parsing and Windows shell actions."""
from __future__ import annotations

import datetime as _dt
import fnmatch
import os
import re
import subprocess
import sys
import unicodedata

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def format_size(n):
    """512 -> '512 bytes'; 1536 -> '1.5 KB'; 5*1024**2 -> '5.00 MB'."""
    if n is None:
        return ""
    n = int(n)
    if n < 1024:
        return "%d bytes" % n
    if n < 1024 ** 2:
        return "%.1f KB" % (n / 1024.0)
    if n < 1024 ** 3:
        return "%.2f MB" % (n / 1024.0 ** 2)
    if n < 1024 ** 4:
        return "%.2f GB" % (n / 1024.0 ** 3)
    return "%.2f TB" % (n / 1024.0 ** 4)


def format_ts(ts):
    """Unix timestamp -> 'YYYY-MM-DD HH:MM:SS' in local time."""
    if not ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def normalize_diacritics(s):
    """'cafe' -> 'cafe' (used by the Match Diacritics option)."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------------------
# size expressions:  size:>10mb   size:10mb..20mb   size:<1gb
# ---------------------------------------------------------------------------

_SIZE_UNITS = {
    "": 1, "b": 1, "byte": 1, "bytes": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
    "t": 1024 ** 4, "tb": 1024 ** 4, "tib": 1024 ** 4,
}


def parse_size(text):
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*", text or "")
    if not m:
        raise ValueError("invalid size: %r" % (text,))
    unit = m.group(2).lower()
    if unit not in _SIZE_UNITS:
        raise ValueError("invalid size unit: %r" % unit)
    return int(float(m.group(1)) * _SIZE_UNITS[unit])


def parse_size_expr(text):
    """Return (op, a, b) with op in eq/gt/lt/ge/le/range."""
    text = (text or "").strip()
    if ".." in text:
        a, b = text.split("..", 1)
        return ("range", parse_size(a), parse_size(b))
    for op in (">=", "<=", "==", ">", "<", "="):
        if text.startswith(op):
            name = {">=": "ge", "<=": "le", "==": "eq", "=": "eq",
                    ">": "gt", "<": "lt"}[op]
            return (name, parse_size(text[len(op):]), None)
    return ("eq", parse_size(text), None)


# ---------------------------------------------------------------------------
# date expressions:  dm:today  dm:yesterday  dm:thisweek  dm:2024-01-01
#                    dm:>2024-01-01  dm:2024-01-01..2024-02-01  dm:past7days
# ---------------------------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%Y-%m", "%Y/%m",
)

_DATE_ONLY_RE = re.compile(r"^\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|\d{4}[-/]\d{1,2})\s*$")


def _parse_dt(text):
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("invalid date: %r" % (text,))


def _is_date_only(text):
    return bool(_DATE_ONLY_RE.match(text or ""))


def _midnight(d):
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_week(d):
    d = _midnight(d)
    return d - _dt.timedelta(days=d.weekday())  # Monday


def _start_of_month(d):
    return _midnight(d.replace(day=1))


def _next_month(d):
    return _start_of_month(d.replace(day=28) + _dt.timedelta(days=5))


def _start_of_year(d):
    return _midnight(d.replace(month=1, day=1))


def parse_date_expr(text, now=None):
    """Return (lo, hi) epoch bounds; None means unbounded."""
    now = now or _dt.datetime.now()
    t = (text or "").strip()
    low = t.lower()

    rolling = {"pastweek": 7, "past7days": 7,
               "pastmonth": 30, "past30days": 30,
               "pastyear": 365, "past365days": 365}
    if low in rolling:
        lo = _midnight(now) - _dt.timedelta(days=rolling[low])
        return (lo.timestamp(), None)
    if low == "today":
        lo = _midnight(now)
        return (lo.timestamp(), (lo + _dt.timedelta(days=1)).timestamp())
    if low == "yesterday":
        lo = _midnight(now) - _dt.timedelta(days=1)
        return (lo.timestamp(), _midnight(now).timestamp())
    if low == "thisweek":
        lo = _start_of_week(now)
        return (lo.timestamp(), (lo + _dt.timedelta(days=7)).timestamp())
    if low == "lastweek":
        lo = _start_of_week(now) - _dt.timedelta(days=7)
        return (lo.timestamp(), (lo + _dt.timedelta(days=7)).timestamp())
    if low == "thismonth":
        lo = _start_of_month(now)
        return (lo.timestamp(), _next_month(lo).timestamp())
    if low == "lastmonth":
        hi = _start_of_month(now)
        lo = _start_of_month(hi - _dt.timedelta(days=1))
        return (lo.timestamp(), hi.timestamp())
    if low == "thisyear":
        lo = _start_of_year(now)
        return (lo.timestamp(), lo.replace(year=lo.year + 1).timestamp())
    if low == "lastyear":
        hi = _start_of_year(now)
        lo = hi.replace(year=hi.year - 1)
        return (lo.timestamp(), hi.timestamp())

    if ".." in t:
        a, b = t.split("..", 1)
        lo = _parse_dt(a).timestamp()
        bd = _parse_dt(b)
        hi = bd.timestamp() + 86400.0 if _is_date_only(b) else bd.timestamp()
        return (lo, hi)

    for op in (">=", "<=", ">", "<", "=", "=="):
        if t.startswith(op):
            v = _parse_dt(t[len(op):]).timestamp()
            if op in (">", ">="):
                return (v, None)
            if op in ("<", "<="):
                return (None, v)
            return (v, v)

    v = _parse_dt(t)
    lo = v.timestamp()
    hi = lo + 86400.0 if _is_date_only(t) else lo
    return (lo, hi)


# ---------------------------------------------------------------------------
# Windows shell actions
# ---------------------------------------------------------------------------

def open_file(path):
    """Open a file/folder with its default application."""
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606 - deliberate shell open
    else:
        subprocess.Popen(["xdg-open", path])


def open_folder(path):
    if IS_WINDOWS:
        subprocess.Popen(["explorer", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_path(path):
    """Open the parent folder with the item selected in Explorer."""
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", path])
    else:
        open_folder(os.path.dirname(path))


def show_properties(path):
    """Show the Windows shell Properties dialog. Returns True on success."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        SEE_MASK_INVOKEIDLIST = 0x0000000C
        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = SEE_MASK_INVOKEIDLIST
        sei.lpVerb = "properties"
        sei.lpFile = path
        sei.nShow = 5
        return bool(ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)))
    except Exception:
        return False


def recycle(paths):
    """Move files/folders to the Recycle Bin. Returns True on success."""
    if not IS_WINDOWS or not paths:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hWnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.USHORT), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        buf = "\0".join(paths) + "\0\0"
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = buf
        op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


def fixed_drives():
    """List of fixed drive roots, e.g. ['C:\\', 'D:\\']."""
    if not IS_WINDOWS:
        return []
    try:
        import ctypes
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                root = "%s:\\" % chr(ord("A") + i)
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    drives.append(root)
        return drives
    except Exception:
        return []


def match_exclude(full_path, patterns):
    """True if full_path matches any exclude pattern (paths or wildcards).

    Wildcard patterns also cover the whole subtree below a match, so
    ``*\\Windows.old`` excludes the whole tree, not just the folder itself.
    """
    if not patterns:
        return False
    low = full_path.lower().rstrip("\\")
    for pat in patterns:
        p = (pat or "").strip().lower().rstrip("\\")
        if not p:
            continue
        if low == p or low.startswith(p + "\\"):
            return True
        if fnmatch.fnmatch(low, p):
            return True
        if fnmatch.fnmatch(low, p + "\\*"):
            return True
    return False
