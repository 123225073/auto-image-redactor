@echo off
chcp 65001 >nul
pushd "%~dp0"

echo.
echo CSDN image privacy tool
echo ----------------------------------------
echo Drag a Markdown or HTML file into this window, then press Enter.
echo.
set /p SOURCE_FILE=File path:

if "%SOURCE_FILE%"=="" (
  echo No file path entered.
  pause
  popd
  exit /b 1
)

set SOURCE_FILE=%SOURCE_FILE:"=%

echo.
echo Checking dependencies...
set PIP_LOG=%TEMP%\csdn-safe-pip-install.log
python -m pip install --disable-pip-version-check --quiet -r requirements.txt >nul 2>"%PIP_LOG%"
if errorlevel 1 (
  echo Dependency installation failed.
  type "%PIP_LOG%"
  pause
  popd
  exit /b 1
)

echo.
echo Processing images...
python csdn_image_mosaic.py "%SOURCE_FILE%" --mode local

echo.
pause
popd
