@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py --port 8765 --portable
  goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py --port 8765 --portable
  goto :eof
)
echo No se encontro Python 3.
pause
