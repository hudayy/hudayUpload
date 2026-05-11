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

echo.
echo Looking for Inno Setup compiler (ISCC)...
set ISCC_PATH=
for %%D in (
    "%ProgramFiles(x86)%\Inno Setup 6"
    "%ProgramFiles%\Inno Setup 6"
    "%ProgramFiles(x86)%\Inno Setup 5"
    "%ProgramFiles%\Inno Setup 5"
) do (
    if exist "%%~D\ISCC.exe" (
        set "ISCC_PATH=%%~D\ISCC.exe"
        goto :found_iscc
    )
)
echo Inno Setup not found -- skipping installer build.
echo To build the installer, install Inno Setup from https://jrsoftware.org/isinfo.php
goto :done

:found_iscc
echo Found ISCC at: %ISCC_PATH%
echo Building installer...
"%ISCC_PATH%" installer.iss
if exist "dist\hudayUpload_Setup_*.exe" (
    echo Installer built successfully.
) else (
    echo Installer build may have failed -- check output above.
)

:done
echo.
echo Done.
pause
