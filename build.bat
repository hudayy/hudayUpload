@echo off
setlocal

echo Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo Generating icon.ico...
python make_icon.py

echo.
echo Building hudayUpload.exe...
pyinstaller hudayUpload.spec --noconfirm

echo.
if not exist "dist\hudayUpload.exe" (
    echo Build FAILED -- check output above.
    pause
    exit /b 1
)
echo Build successful: dist\hudayUpload.exe

:done
echo.
echo Done.
pause
