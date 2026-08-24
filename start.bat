@echo off
setlocal
rem ============================================================
rem  QQ Bot v3 launcher (Windows edition) - CONSOLE DEBUG MODE
rem  - Normal users: double-click start_gui.vbs (no window)
rem  - This script:  runs in a console window so you can see
rem    all startup logs. Closing the window stops the GUI.
rem  NOTE: keep this file pure ASCII + CRLF (no Chinese, no LF)
rem ============================================================
cd /d "%~dp0"
if not exist data\run mkdir data\run
if not exist "%USERPROFILE%\Desktop\QQ Bot.lnk" goto mkl
goto run
:mkl
echo Creating desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"
:run
echo.
echo Starting QQ Bot v3 GUI (console mode - logs shown below)...
echo To stop: close this window.
echo.
python\python.exe gui_launcher.py
echo.
echo GUI exited (code %errorlevel%).
pause
