"""Remove AQI Dashboard from Windows startup."""
import sys

APP_NAME = "AQIDashboard"

def main():
    if sys.platform != "win32":
        print("This script only runs on Windows.")
        return 1

    import winreg as reg
    key = reg.OpenKey(reg.HKEY_CURRENT_USER,
                      r"Software\Microsoft\Windows\CurrentVersion\Run",
                      0, reg.KEY_SET_VALUE)
    try:
        reg.DeleteValue(key, APP_NAME)
        print(f"✓ Removed startup entry: {APP_NAME}")
    except FileNotFoundError:
        print(f"No entry named '{APP_NAME}' found.")
    finally:
        reg.CloseKey(key)
    return 0

if __name__ == "__main__":
    sys.exit(main())
