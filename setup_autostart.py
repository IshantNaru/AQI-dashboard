"""Add AQI Dashboard to Windows startup via HKCU registry Run key."""
import sys

from autostart import build_command, write_entry


def main():
    if sys.platform != "win32":
        print("This script only runs on Windows.")
        return 1

    cmd = build_command()
    write_entry(cmd)
    print(f"✓ Installed: {cmd}")
    print("Dashboard will launch on next login.")
    print("If you move this folder, just open the dashboard once — it re-points the entry itself.")
    print("To remove: python uninstall_autostart.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
