@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "C:\Python314\pythonw.exe" "app.py"
