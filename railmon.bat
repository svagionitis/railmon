@echo off
setlocal

rem Ensure console output code page is UTF-8 for Braille and Greek Unicode support
chcp 65001 >nul 2>&1

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%~dp0railmon" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py "%~dp0railmon" %*
    exit /b %ERRORLEVEL%
)

echo [ERROR] Python was not found in PATH. Please install Python 3.10+ from https://python.org >&2
exit /b 1
