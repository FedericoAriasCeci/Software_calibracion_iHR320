@echo off
setlocal
rem Ajusta los barridos de rendijas con el entorno pump_probe_env.
rem   Doble clic: procesa todos los barrido_rendijas_*.csv de esta carpeta.
rem   Arrastrar uno o varios CSV sobre este archivo: procesa solo esos.
cd /d "%~dp0"
set "ENTORNO=%USERPROFILE%\.conda\envs\pump_probe_env"
if not exist "%ENTORNO%\python.exe" goto sin_entorno
if exist "C:\ProgramData\Anaconda3\Scripts\activate.bat" call "C:\ProgramData\Anaconda3\Scripts\activate.bat" "%ENTORNO%"
"%ENTORNO%\python.exe" -B "%~dp0ajustar_barrido.py" %*
echo.
pause
exit /b

:sin_entorno
echo No encuentro el entorno en %ENTORNO%
pause
exit /b 1
