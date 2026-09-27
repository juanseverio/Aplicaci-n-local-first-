@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 no esta instalado.
  pause
  exit /b 1
)
py -3 -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1
py -3 -m PyInstaller --clean --noconfirm Presencialidad.spec
if errorlevel 1 exit /b 1
echo.
echo Compilacion terminada: dist\Presencialidad.exe
pause
