@echo off
setlocal
cd /d "%~dp0.."
if exist "spectral_calibration\.venv\Scripts\python.exe" goto horiba_env
if exist ".venv_calibration\Scripts\python.exe" goto local_env
if exist "C:\ProgramData\Anaconda3\python.exe" goto anaconda
python -B -m spectral_calibration gui
goto end
:horiba_env
call "C:\ProgramData\Anaconda3\Scripts\activate.bat" "C:\ProgramData\Anaconda3"
"spectral_calibration\.venv\Scripts\python.exe" -B -m spectral_calibration gui
goto end
:local_env
".venv_calibration\Scripts\python.exe" -B -m spectral_calibration gui
goto end
:anaconda
call "C:\ProgramData\Anaconda3\Scripts\activate.bat" "C:\ProgramData\Anaconda3"
"C:\ProgramData\Anaconda3\python.exe" -B -m spectral_calibration gui
:end
if errorlevel 1 pause
