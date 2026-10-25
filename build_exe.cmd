@echo off
rem Build a portable Maliang folder into dist; does not replace an installed app.
rem First install the build environment with: python -m pip install ".[dev,agent]"
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean maliang.spec
) else (
  py -3 -m PyInstaller --noconfirm --clean maliang.spec
)
exit /b %errorlevel%
