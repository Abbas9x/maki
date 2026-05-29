# Maki Auto-start — Setup & Troubleshooting (V7.5)

Maki uses **two parallel methods** so at least one fires after every reboot:

1. **Startup-folder shortcut** (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Maki.lnk`)
   The most reliable method. Windows always runs items here at login.

2. **Task Scheduler task** (`MakiAutoStart`)
   Adds an additional trigger for **workstation unlock**.

Both methods point to `wscript.exe "<project>\start_maki_hidden.vbs"` which:
- writes a diagnostic log to `logs\startup.log`
- skips launching if Maki is already running (singleton)
- finds the correct python (venv first, then known system locations, then PATH)
- launches `pythonw.exe main.py` with no console window

## Install

```powershell
cd C:\Users\<you>\projectmaki
powershell -ExecutionPolicy Bypass -File .\uninstall_startup_task.ps1
powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1
```

If the PowerShell shortcut creator fails (rare), use the Python fallback:

```powershell
python .\create_startup_shortcut.py
```

## Test without rebooting

Double-click `test_startup_launch.bat`. It runs the exact VBS Windows runs at login
and tails the last 20 lines of `logs\startup.log` so you can see what happened.

## Verify installed

```powershell
# Task Scheduler task
Get-ScheduledTask -TaskName MakiAutoStart

# Startup-folder shortcut
Get-Item "$([Environment]::GetFolderPath('Startup'))\Maki.lnk"
```

## Manual launch (no reboot)

```powershell
& "$env:SystemRoot\System32\wscript.exe" "C:\Users\<you>\projectmaki\start_maki_hidden.vbs"
```

## Troubleshooting

If Maki does **not** appear after restart:

1. **Check the log first**:
   ```
   notepad C:\Users\<you>\projectmaki\logs\startup.log
   ```
   The last entries tell you:
   - whether the VBS fired
   - which python was used
   - whether `WshShell.Run` errored

2. **Open the Startup folder**:
   `Win+R → shell:startup → Enter`. Confirm `Maki.lnk` is there.

3. **Right-click `Maki.lnk` → Properties**:
   - Target should be: `C:\Windows\System32\wscript.exe`
   - Arguments should include `start_maki_hidden.vbs`
   - "Start in" should be the project folder

4. **Test the VBS directly**:
   Double-click `test_startup_launch.bat`. If that works but Startup folder doesn't,
   the shortcut itself is broken — re-run `create_startup_shortcut.py`.

5. **Python not on PATH at login**?
   The VBS now searches the venv first and several common install paths, but if you
   have a non-standard Python install, edit `start_maki_hidden.vbs` and add your
   full path to the `candidates` array.

6. **Antivirus blocking the VBS**?
   Some AVs flag any `.vbs` in Startup. Add an exclusion for the project folder.

7. **Task Scheduler "Access Denied"**:
   Use only the Startup-folder shortcut (it doesn't need the task). The shortcut alone
   fires reliably at every login.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_startup_task.ps1
```

## What the watcher does

When Maki is running and you unlock the PC or wake from sleep,
`wake_unlock_watcher.py` greets you (e.g. *"Welcome back, <your name>."*).
This works without restarting Maki. The unlock detection uses
`OpenInputDesktop`; the wake-from-sleep detection uses a wall-clock
gap heuristic (>90 s between polls).
