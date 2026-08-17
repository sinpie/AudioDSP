@echo off
setlocal
set "SCRIPT=%~dp0verify_audiodsp_pi4_pi5.ps1"
if not exist "%SCRIPT%" (
  echo Missing script: %SCRIPT%
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
if errorlevel 1 (
  echo.
  echo Verification failed. Review the output above.
  pause
  exit /b 1
)
echo.
echo AudioDSP Pi 4/5 verification passed.
pause
endlocal
