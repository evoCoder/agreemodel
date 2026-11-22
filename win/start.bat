@echo off
rem Simulator "Soglasie" - portable version. No installation, no internet needed.
cd /d "%~dp0"
"%~dp0python\python.exe" "%~dp0serve.py" %*
if errorlevel 1 pause
