@echo off
REM ==========================================================================
REM  VeriFlow Studio - one-click Windows build (PyInstaller, single .exe)
REM  Output: dist\VeriFlowStudio.exe   (sample_project is bundled; on first
REM  launch it is copied to Documents\VeriFlow Studio\sample_project)
REM ==========================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul || (echo [ERROR] Python 3.11+ is not on PATH. & goto :fail)

echo === Installing requirements ===
python -m pip install --upgrade -r requirements.txt || goto :fail

echo === Syntax check ===
python -m py_compile config_manager.py verilog_parser.py verilog_runner.py package_manager.py updater.py welcome.py intellisense.py ui_components.py main.py || goto :fail

echo === Building VeriFlowStudio.exe ===
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name VeriFlowStudio ^
    --collect-data customtkinter ^
    --hidden-import pygments.lexers.hdl ^
    --add-data "sample_project;sample_project" ^
    main.py || goto :fail

echo.
echo === Done: %~dp0dist\VeriFlowStudio.exe ===
exit /b 0

:fail
echo.
echo *** BUILD FAILED ***
pause
exit /b 1
