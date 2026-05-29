@echo off
title Maki Debug Console
cd /d "%~dp0"
echo =========================================
echo  Maki Debug Mode — full console logging
echo =========================================
echo.
python main.py
echo.
echo Maki exited. Press any key to close.
pause >nul
