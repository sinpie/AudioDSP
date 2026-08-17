@echo off
setlocal
set "SCRIPT=%~dp0verify_audiodsp_pi2.ps1"
if not exist "%SCRIPT%" (
  echo Missing script: %SCRIPT%
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
if errorlevel 1 (
  echo.
  echo Verification failed. If .local lookup is unavailable, run verify_audiodsp_pi2.ps1 with -PiHost and the router DHCP address.
  pause
  exit /b 1
)
echo.
echo AudioDSP Pi 2 verification passed.
pause
endlocal
