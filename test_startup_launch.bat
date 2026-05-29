@echo off
REM test_startup_launch.bat — visible startup diagnostic.
REM Runs the exact VBS Windows will fire at login, then shows both logs.
setlocal
cd /d "%~dp0"

echo ================================================================
echo  Maki startup diagnostic
echo ================================================================
echo Project       : %CD%
echo VBS path      : %CD%\start_maki_hidden.vbs
echo Launcher log  : %CD%\logs\launcher.log   (written by VBS)
echo Main log      : %CD%\logs\startup.log    (written by main.py)
echo.

if not exist "start_maki_hidden.vbs" (
    echo ERROR: start_maki_hidden.vbs is missing.
    pause
    exit /b 1
)

if not exist "logs" mkdir logs

echo Running the VBS Windows will run at login...
"%SystemRoot%\System32\wscript.exe" "%CD%\start_maki_hidden.vbs"

echo.
echo Waiting 6s for python to come up...
ping -n 7 127.0.0.1 >nul

echo.
echo --- last 30 lines of logs\launcher.log (VBS side) ---
if exist "logs\launcher.log" (
    powershell -NoProfile -Command "Get-Content -Path 'logs\launcher.log' -Tail 30"
) else (
    echo (no launcher log written - VBS failed before logging started)
)
echo.
echo --- last 30 lines of logs\startup.log (Python side) ---
if exist "logs\startup.log" (
    powershell -NoProfile -Command "Get-Content -Path 'logs\startup.log' -Tail 30"
) else (
    echo (no main log written - main.py did not start)
)
echo.

echo --- pythonw.exe processes running with main.py ---
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' } | Format-Table ProcessId, CreationDate, CommandLine -AutoSize"

echo.
echo Look for:
echo   VBS_STARTED        - VBS launcher fired
echo   Python path exists - the python.exe Maki tried to use was found
echo   RUN_COMMAND        - the actual command that ran
echo   MAIN_STARTED       - main.py loaded (Python side)
echo.
echo If Maki did NOT appear, the missing line tells you where it broke.
echo.
pause
endlocal
