@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0runtime\windows-python\python.exe" (
  echo Offline Windows development runtime is missing. Use the complete handoff project.
  exit /b 1
)
"%~dp0runtime\windows-python\python.exe" -B "%~dp0tools\windows_dev.py" %*
set "OCR_DEV_RESULT=%ERRORLEVEL%"
if not "%OCR_DEV_RESULT%"=="0" pause
exit /b %OCR_DEV_RESULT%
