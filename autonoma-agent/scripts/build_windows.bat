@echo off
REM Compila Autonoma.exe con PyInstaller (ejecutar desde la raíz del repo).
cd /d "%~dp0\.."
python -m pip install ".[build,keyboard]"
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean autonoma.spec
if errorlevel 1 exit /b 1
echo.
echo Listo: dist\Autonoma.exe
echo Coloca un archivo .env junto al .exe con NOTRACK_API_KEY=...
pause
