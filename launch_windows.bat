@echo off
setlocal DisableDelayedExpansion

rem FTHR Clips development launcher.
rem Prefer the live checkout so a shortcut always launches the newest source
rem code. Fall back to the portable or installed build when source mode is
rem unavailable.

set "ROOT=%~dp0"
cd /d "%ROOT%"

rem Prefer the project virtual environment, then a PATH-provided pythonw.
set "PYTHONW=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW="
if not defined PYTHONW (
    for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYTHONW set "PYTHONW=%%P"
)

rem Query the same source discovery as MainWindow without importing the UI.
rem Expand paths in the child shell so workspace punctuation stays quoted.
set "ENGINE_PATH="
set "ENGINE_DISCOVERY=%ROOT%FTHR_UI\core\engine_discovery.py"
if defined PYTHONW if exist "%ENGINE_DISCOVERY%" (
    for /f "delims=" %%P in ('cmd.exe /d /v:on /c ^""!PYTHONW!" -B "!ENGINE_DISCOVERY!"^" 2^>nul') do set "ENGINE_PATH=%%P"
)

rem Report a known playback bridge for diagnostics. The source runtime chooses
rem the newest existing build from these locations.
set "MIXER_PATH="
for %%P in (
    "%ROOT%FTHRcapture\FTHRPlaybackMixer\x64\Upgrade\FTHRPlaybackMixer.dll"
    "%ROOT%FTHRcapture\FTHRPlaybackMixer\x64\Release\FTHRPlaybackMixer.dll"
    "%ROOT%FTHRcapture\x64\Release\FTHRPlaybackMixer.dll"
) do if not defined MIXER_PATH if exist "%%~P" set "MIXER_PATH=%%~P"

if defined ENGINE_PATH (
    if /i "%~1"=="--diagnose" goto diagnose_source
    start "" /d "%ROOT%" "%PYTHONW%" "%ROOT%FTHR_UI\main.py" %*
    exit /b 0
)

rem No usable source runtime: use the locally built portable bundle next.
set "PORTABLE_EXE=%ROOT%dist\FTHRClips\FTHRClips.exe"
if exist "%PORTABLE_EXE%" (
    if /i "%~1"=="--diagnose" (
        setlocal EnableDelayedExpansion
        echo FTHR Clips launcher mode: portable bundle
        echo Executable: !PORTABLE_EXE!
        exit /b 0
    )
    start "" /d "%ROOT%dist\FTHRClips" "%PORTABLE_EXE%" %*
    exit /b 0
)

rem Finally use the default install location when the application is installed.
set "INSTALLED_EXE=%ProgramFiles%\FTHRClips\FTHRClips.exe"
if exist "%INSTALLED_EXE%" (
    if /i "%~1"=="--diagnose" (
        setlocal EnableDelayedExpansion
        echo FTHR Clips launcher mode: installed build
        echo Executable: !INSTALLED_EXE!
        exit /b 0
    )
    start "" /d "%ProgramFiles%\FTHRClips" "%INSTALLED_EXE%" %*
    exit /b 0
)

echo FTHR Clips could not be launched.
echo.
echo Build the Windows engine first, or create the portable bundle with:
echo   python -m PyInstaller FTHR.spec --clean
echo.
if /i not "%~1"=="--diagnose" pause
exit /b 1

:diagnose_source
setlocal EnableDelayedExpansion
echo FTHR Clips launcher mode: source checkout
echo Python: !PYTHONW!
echo Engine: !ENGINE_PATH!
if defined MIXER_PATH echo Mixer: !MIXER_PATH!
exit /b 0
