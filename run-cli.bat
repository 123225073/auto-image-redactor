@echo off
setlocal
cd /d "%~dp0"
python auto_image_redactor_cli.py %*
