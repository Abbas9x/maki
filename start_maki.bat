@echo off
REM start_maki.bat — visible launcher (writes to logs\startup.log too)
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir logs

echo [%DATE% %TIME%]  start_maki.bat invoked >> "logs\startup.log"
echo [%DATE% %TIME%]  CWD: %CD% >> "logs\startup.log"

REM Prefer venv python; otherwise fall back to system pythonw.exe
set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

echo [%DATE% %TIME%]  Using: %PYTHONW% >> "logs\startup.log"
start "" "%PYTHONW%" "main.py"
echo [%DATE% %TIME%]  launched. >> "logs\startup.log"
endlocal
