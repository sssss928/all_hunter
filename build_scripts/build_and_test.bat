@echo off
setlocal
cd /d "%~dp0.."

set "VERSION=%~1"
if "%VERSION%"=="" set "VERSION=1.0.1"

echo Building Tickets Hunter Windows release v%VERSION%...
powershell -NoProfile -ExecutionPolicy Bypass -File "build_scripts\build_release.ps1" -Version "%VERSION%"
if errorlevel 1 (
    echo.
    echo [ERROR] Release build failed.
    exit /b 1
)

echo.
echo [OK] Release package is available under dist\release\
exit /b 0

