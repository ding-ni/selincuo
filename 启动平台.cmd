@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 请安装 Python 并按 README.md 安装依赖，或运行已交付 EXE。
  pause
  exit /b 1
)
python app_entry.py
if errorlevel 1 pause
