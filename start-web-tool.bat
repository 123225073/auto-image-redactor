@echo off
pushd "%~dp0"

set PIP_LOG=%TEMP%\csdn-safe-web-pip-install.log
echo CSDN image privacy web tool
echo ----------------------------------------
echo Checking dependencies...
python -m pip install --disable-pip-version-check --quiet -r requirements.txt >nul 2>"%PIP_LOG%"
if errorlevel 1 (
  echo Dependency installation failed.
  type "%PIP_LOG%"
  pause
  popd
  exit /b 1
)

echo Starting local web tool...
start "" "http://127.0.0.1:8866"
python web_tool.py

pause
popd
