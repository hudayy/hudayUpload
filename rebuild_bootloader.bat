@echo off
setlocal

:: rebuild_bootloader.bat
::
:: Compiles the PyInstaller bootloader from source so it produces a unique
:: binary that AV engines don't have a signature for.  Run this ONCE before
:: build.bat whenever you upgrade PyInstaller or want a fresh bootloader.
::
:: Requirements:
::   - Git (git.exe in PATH)
::   - A C compiler in PATH — either:
::       Visual Studio / Build Tools: run this from a "Developer Command Prompt"
::       or manually call vcvarsall.bat (e.g. "C:\Program Files\Microsoft Visual
::       Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64)
::       -- OR --
::       MinGW-w64: install via https://winlibs.com and add bin\ to PATH

echo ================================================
echo  PyInstaller bootloader rebuild
echo ================================================
echo.

:: Verify git
where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: git not found in PATH.
    echo Install Git from https://git-scm.com and rerun.
    pause & exit /b 1
)

:: Verify a C compiler (cl.exe or gcc)
where cl >nul 2>&1
if not errorlevel 1 goto :have_compiler
where gcc >nul 2>&1
if not errorlevel 1 goto :have_compiler
echo ERROR: No C compiler found ^(cl.exe or gcc^).
echo Run this script from a Visual Studio Developer Command Prompt,
echo or install MinGW-w64 and add its bin\ folder to PATH.
pause & exit /b 1

:have_compiler

:: Clone PyInstaller at the same version currently installed
for /f "delims=" %%V in ('python -c "import PyInstaller; print(PyInstaller.__version__)"') do set PYIVER=%%V
echo Installed PyInstaller: %PYIVER%
echo.

if exist pyinstaller_src (
    echo Removing old pyinstaller_src...
    rmdir /s /q pyinstaller_src
)

echo Cloning PyInstaller %PYIVER%...
git clone --depth 1 --branch v%PYIVER% https://github.com/pyinstaller/pyinstaller pyinstaller_src
if errorlevel 1 (
    echo.
    echo Tag v%PYIVER% not found -- trying to clone default branch instead.
    git clone --depth 1 https://github.com/pyinstaller/pyinstaller pyinstaller_src
    if errorlevel 1 goto :fail
)

echo.
echo Building bootloader (64-bit)...
cd pyinstaller_src\bootloader
python waf all --target-arch=64bit
if errorlevel 1 goto :fail_build

cd ..\..
echo.
echo Installing rebuilt PyInstaller into current Python environment...
pip install .\pyinstaller_src --quiet
if errorlevel 1 goto :fail

echo.
echo ================================================
echo  Bootloader rebuilt successfully.
echo  Run build.bat to produce the release exe.
echo ================================================
echo.
goto :done

:fail_build
cd ..\..
:fail
echo.
echo ================================================
echo  Bootloader build FAILED -- see output above.
echo ================================================
pause
exit /b 1

:done
pause
