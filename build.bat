@echo off
setlocal

echo Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo Building hudayUpload...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "hudayUpload" ^
    --icon "assets\icon.png" ^
    --add-data "assets;assets" ^
    main.py

echo.
if exist "dist\hudayUpload.exe" (
    echo Build successful!
    echo Output: dist\hudayUpload.exe
) else (
    echo Build may have failed -- check output above.
)

pause
