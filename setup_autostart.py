"""Add AQI Dashboard to Windows startup via HKCU registry Run key."""
import os, sys

APP_NAME = "AQIDashboard"

def main():
    if sys.platform != "win32":
        print("This script only runs on Windows.")
        return 1

    import winreg as reg
    here   = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "dashboard.py")
    if not os.path.exists(script):
        print(f"Cannot find dashboard.py at {script}")
        return 1

    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    cmd = f'"{pyw}" "{script}"'

    key = reg.OpenKey(reg.HKEY_CURRENT_USER,
                      r"Software\Microsoft\Windows\CurrentVersion\Run",
                      0, reg.KEY_SET_VALUE)
    reg.SetValueEx(key, APP_NAME, 0, reg.REG_SZ, cmd)
    reg.CloseKey(key)

    print(f"✓ Installed: {cmd}")
    print("Dashboard will launch on next login.")
    print("To remove: python uninstall_autostart.py")
    return 0

if __name__ == "__main__":
    sys.exit(main())
