"""
Windows autostart helpers (HKCU\\...\\Run).

ensure_autostart() runs on every launch and *repairs* an existing startup
entry if the project folder has moved. It never creates an entry that isn't
there (so uninstall_autostart.py stays respected), and it keeps the
interpreter already registered unless that interpreter no longer exists.
"""
import os
import re
import sys

APP_NAME = "AQIDashboard"
RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
SCRIPT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")


def _windowless(exe: str) -> str:
    """python.exe -> pythonw.exe (same folder) if it exists."""
    folder, name = os.path.split(exe)
    if name.lower() == "python.exe":
        pyw = os.path.join(folder, "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
    return exe


def build_command(interpreter: str | None = None) -> str:
    if getattr(sys, "frozen", False):          # future PyInstaller build
        return f'"{sys.executable}"'
    return f'"{_windowless(interpreter or sys.executable)}" "{SCRIPT}"'


def _parse(cmd: str) -> list[str]:
    return re.findall(r'"([^"]+)"', cmd or "")


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def read_entry() -> str | None:
    import winreg as reg
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return reg.QueryValueEx(k, APP_NAME)[0]
    except FileNotFoundError:
        return None


def write_entry(cmd: str) -> None:
    import winreg as reg
    with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as k:
        reg.SetValueEx(k, APP_NAME, 0, reg.REG_SZ, cmd)


def ensure_autostart() -> str:
    """Returns a short status string; never raises."""
    if sys.platform != "win32":
        return "skipped (not Windows)"
    try:
        current = read_entry()
        if current is None:
            return "no entry (autostart not installed)"

        parts = _parse(current)
        if getattr(sys, "frozen", False):
            desired = build_command()
        else:
            old_interp = parts[0] if parts else None
            keep = old_interp if old_interp and os.path.exists(old_interp) else None
            desired = build_command(keep)

        if [_norm(p) for p in parts] == [_norm(p) for p in _parse(desired)]:
            return "ok"
        write_entry(desired)
        return f"repaired: {current}  ->  {desired}"
    except Exception as e:
        return f"error: {e}"
