@echo off
pushd "%~dp0"

set PIP_LOG=%TEMP%\csdn-safe-web-pip-install.log
echo CSDN image privacy web tool
echo ----------------------------------------
where python >nul 2>nul
if not errorlevel 1 (
  echo Checking dependencies with Python...
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
  exit /b 0
)

echo Starting local web tool...
start "" "http://127.0.0.1:8866"
where uv >nul 2>nul
if errorlevel 1 (
  echo Neither python nor uv was found. Please install Python 3.11+ or uv, then run this file again.
  pause
  popd
  exit /b 1
)
set UV_CACHE_DIR=%CD%\.uv-cache
uv run --python 3.11 --with-requirements requirements.txt python web_tool.py

pause
popd
