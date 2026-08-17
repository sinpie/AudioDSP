@echo off
setlocal
set "SCRIPT=%~dp0write_final_sd_as_admin.ps1"
if not exist "%SCRIPT%" (
  echo Missing script: %SCRIPT%
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File','%SCRIPT%')"
if errorlevel 1 (
  echo Failed to open the Administrator PowerShell window.
  pause
  exit /b 1
)
echo AudioDSP Pi 4/5 SD writer started. Follow the Administrator PowerShell window.
endlocal
