@echo off
REM Build script for Stream Monitor
REM Requires: pip install pyinstaller

echo ========================================
echo Stream Monitor Build Script
echo ========================================
echo.

echo [1/5] Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo [2/5] Generating icon...
python create_icon.py

echo.
echo [3/5] Building Setup Wizard...
pyinstaller --onefile --windowed --name "StreamMonitorSetup" --icon=icon.ico setup_wizard.py

echo.
echo [4/5] Building Settings Editor...
pyinstaller --onefile --windowed --name "StreamMonitorSettings" --icon=icon.ico settings_editor.py

echo.
echo [5/5] Building Tray Application...
pyinstaller --onefile --windowed --name "StreamMonitor" --icon=icon.ico stream_monitor_tray.py

echo.
echo ========================================
echo Build complete!
echo ========================================
echo.
echo Executables are in the 'dist' folder:
echo   - StreamMonitor.exe (tray app)
echo   - StreamMonitorSetup.exe (setup wizard)
echo   - StreamMonitorSettings.exe (settings editor)
echo.
echo Next steps to create installer:
echo 1. Install Inno Setup from https://jrsoftware.org/isinfo.php
echo 2. Open installer.iss in Inno Setup Compiler
echo 3. Click Build ^> Compile
echo 4. Installer will be in 'installer_output' folder
echo.
pause
