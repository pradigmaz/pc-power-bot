@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PCPB_ROOT=%~dp0"
set "PCPB_SYSTEM32=%SystemRoot%\System32"
set "PCPB_PS=%PCPB_SYSTEM32%\WindowsPowerShell\v1.0\powershell.exe"
set "PCPB_PYTHON="
set "PCPB_TASK_EXISTS="
set "PCPB_PUSHED="
set "PCPB_ENV_FILE_READY="

pushd "%PCPB_ROOT%" >nul || goto :failed
set "PCPB_PUSHED=1"

echo.
echo === PC Power Bot preflight ===
for %%F in (bot.py install-autostart.ps1 pc_power_bot\config.py tests\test_power_bot.py) do (
    if not exist "%%F" (
        echo [ERROR] Missing required file: %%F
        goto :failed
    )
)

for /f "delims=" %%P in ('where.exe python 2^>nul') do if not defined PCPB_PYTHON set "PCPB_PYTHON=%%P"
if not defined PCPB_PYTHON for /f "delims=" %%P in ('where.exe py 2^>nul') do if not defined PCPB_PYTHON set "PCPB_PYTHON=%%P"
if not defined PCPB_PYTHON (
    echo [ERROR] Python 3.10 or newer is required. Install Python and run this file again.
    goto :failed
)

"%PCPB_PYTHON%" --version

if exist ".env" set "PCPB_ENV_FILE_READY=1"
if not defined PCPB_ENV_FILE_READY echo [NOTE] .env is missing. The config check can only pass with environment variables.

echo.
echo [1/5] Python compilation...
"%PCPB_PYTHON%" -m compileall -q . || goto :failed

echo [2/5] Unit tests...
"%PCPB_PYTHON%" -m unittest discover -s tests -v || goto :failed

echo [3/5] Checking .env without Telegram or power actions...
"%PCPB_PYTHON%" bot.py --check || goto :failed

echo [4/5] Checking the Task Scheduler service...
"%PCPB_PS%" -NoProfile -NonInteractive -Command "if ((Get-Service -Name Schedule).Status -ne 'Running') { exit 1 }"
if errorlevel 1 (
    echo [ERROR] The Task Scheduler service is not running.
    goto :failed
)

echo [5/5] Available power states:
"%PCPB_SYSTEM32%\powercfg.exe" /a
if errorlevel 1 echo [WARNING] powercfg /a did not complete. Check sleep manually.

"%PCPB_SYSTEM32%\schtasks.exe" /Query /TN "\PcPowerBot" >nul 2>&1
if not errorlevel 1 (
    set "PCPB_TASK_EXISTS=1"
    echo.
    echo Autostart task \PcPowerBot already exists.
)

echo.
echo Preflight passed. The bot, Telegram, and power actions were not started.
if not defined PCPB_ENV_FILE_READY (
    echo Autostart is not offered until .env exists next to bot.py.
    goto :done
)
if defined PCPB_TASK_EXISTS (
    choice /C UE /N /M "[U] Update autostart and start bot  [E] Exit"
    if errorlevel 2 goto :done

    "%PCPB_PS%" -NoProfile -NonInteractive -File "%PCPB_ROOT%install-autostart.ps1" -PythonPath "%PCPB_PYTHON%" -ReplaceExisting
    if errorlevel 1 (
        echo [ERROR] Autostart was not updated. See the PowerShell message above.
        goto :failed
    )
    echo Autostart updated. The bot starts without a console window.
    goto :done
)

choice /C IE /N /M "[I] Install autostart  [E] Exit"
if errorlevel 2 goto :done

choice /C YN /N /M "Create the current-user autostart task? [Y/N]"
if errorlevel 2 goto :done

"%PCPB_PS%" -NoProfile -NonInteractive -File "%PCPB_ROOT%install-autostart.ps1" -PythonPath "%PCPB_PYTHON%"
if errorlevel 1 (
    echo [ERROR] Autostart was not installed. See the PowerShell message above.
    goto :failed
)
echo Autostart installed. The bot starts now without a console window.

:done
popd >nul
echo.
pause
exit /b 0

:failed
if defined PCPB_PUSHED popd >nul 2>&1
echo.
echo Preflight stopped. Autostart was not changed.
pause
exit /b 1
