@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\Presencialidad.exe" (
  echo Primero ejecuta BUILD_WINDOWS_EXE.bat
  pause
  exit /b 1
)
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo No se encontro Inno Setup 6.
  echo Instala Inno Setup y vuelve a ejecutar este archivo.
  pause
  exit /b 1
)
"%ISCC%" installer.iss
pause
