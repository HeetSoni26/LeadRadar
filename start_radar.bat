@echo off
title Lead Radar
cd /d "%~dp0"
echo ============================================
echo   Lead Radar - watching for leads 24x7
echo   Stop = close this window (or Ctrl+C)
echo ============================================
python radar.py
echo.
echo Lead Radar stopped.
pause
