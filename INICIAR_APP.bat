@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py --port 8765
  goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py --port 8765
  goto :eof
)
echo.
echo No se encontro Python 3 en el sistema.
echo Para una instalacion sin Python, compila Presencialidad.exe con BUILD_WINDOWS_EXE.bat.
echo.
pause
