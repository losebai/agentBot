@echo off
setlocal
REM CMD 入口复用 PowerShell 的清单和校验流程，并将失败状态返回调用者。
set "INSTALL_SCRIPT=%TEMP%\agentbot-install-%RANDOM%-%RANDOM%.ps1"
curl.exe -fsSL -o "%INSTALL_SCRIPT%" https://public.loserbai.cn/agentbot/install.ps1
if errorlevel 1 (
    if exist "%INSTALL_SCRIPT%" del /q "%INSTALL_SCRIPT%"
    echo Failed to download taobot install script.
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_SCRIPT%"
set "INSTALL_RESULT=%ERRORLEVEL%"
del /q "%INSTALL_SCRIPT%"
endlocal & exit /b %INSTALL_RESULT%
