"""
create_startup_shortcut.py — Python-side fallback for creating the
Startup-folder shortcut when PowerShell's WScript.Shell COM is unreliable.

Run once:
    python create_startup_shortcut.py
"""

import os, sys
from pathlib import Path


def main():
    project_dir = Path(__file__).resolve().parent
    vbs         = project_dir / "start_maki_hidden.vbs"
    wscript     = Path(os.environ["SystemRoot"]) / "System32" / "wscript.exe"

    if not vbs.exists():
        print(f"ERROR: missing {vbs}")
        return 1
    if not wscript.exists():
        print(f"ERROR: wscript.exe not found at {wscript}")
        return 1

    # Resolve Startup folder via known shell folder GUID; safer than %APPDATA% guesswork
    try:
        import ctypes
        from ctypes import wintypes
        SHGetKnownFolderPath = ctypes.windll.shell32.SHGetKnownFolderPath
        # FOLDERID_Startup = {B97D20BB-F46A-4C97-BA10-5E3608430854}
        from uuid import UUID
        guid_bytes = UUID("B97D20BB-F46A-4C97-BA10-5E3608430854").bytes_le
        guid = (ctypes.c_byte * 16).from_buffer_copy(guid_bytes)
        ppath = ctypes.c_wchar_p()
        SHGetKnownFolderPath(guid, 0, None, ctypes.byref(ppath))
        startup_dir = Path(ppath.value)
    except Exception:
        startup_dir = Path(os.path.expanduser(r"~\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"))

    if not startup_dir.exists():
        print(f"ERROR: Startup folder not found: {startup_dir}")
        return 1

    shortcut = startup_dir / "Maki.lnk"

    # Use pywin32 to write a .lnk
    try:
        from win32com.client import Dispatch
    except ImportError:
        print("ERROR: pywin32 required for shortcut creation (pip install pywin32).")
        return 1

    shell = Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(str(shortcut))
    sc.Targetpath       = str(wscript)
    sc.Arguments        = f'"{vbs}"'
    sc.WorkingDirectory = str(project_dir)
    sc.WindowStyle      = 7  # minimized
    sc.Description      = "Maki personal AI assistant"
    sc.save()

    print(f"OK: created {shortcut}")
    print(f"   target: {wscript}")
    print(f"   args  : {vbs}")
    print(f"   wd    : {project_dir}")
    print()
    print("Maki will now launch at next login.")
    print("Test now:  test_startup_launch.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
