@echo off
setlocal
cd /d "%~dp0"
title GTM Task Bulk Upload
set "PY=py -3"
where py >NUL 2>NUL || set "PY=python"
echo [1/2] Checking / installing packages (first run takes 1-3 min)...
%PY% -m pip install --user --disable-pip-version-check --no-warn-script-location -q -r requirements.txt || goto :err
echo.
echo [2/2] Starting app at http://localhost:8501   (close this window to stop)
start "" /b cmd /c "timeout /t 6 >NUL && start http://localhost:8501"
%PY% launch.py --server.headless true --client.toolbarMode minimal --browser.gatherUsageStats false
pause
goto :eof

:err
echo.
echo *** Setup failed - see messages above. ***
pause
