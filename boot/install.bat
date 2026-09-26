@echo off
rem ===========================================================================
rem Ugo - bootstrap Windows SENZA PowerShell (per PC dove powershell.exe e'
rem bloccato da policy/antivirus). Serve solo cmd.exe e curl (integrato in
rem Windows 10 1803+). Da cmd:
rem
rem   curl -fsSL https://raw.githubusercontent.com/giovannimazza/ugo-local-ai-agent/main/boot/install.bat -o "%TEMP%\ugo-install.bat" && "%TEMP%\ugo-install.bat"
rem
rem Trova (o installa) Python, poi passa la mano a boot/install.py che scarica
rem il sorgente, crea il venv e lancia `ugo setup` + `ugo run`.
rem NOTA: niente label/goto NESSUN blocco a parentesi: il file scaricato via
rem curl arriva con line-ending LF e cmd con le label e' imprevedibile; il
rem flusso lineare con `if not defined` funziona sempre.
rem ===========================================================================
setlocal
set RAW=https://raw.githubusercontent.com/giovannimazza/ugo-local-ai-agent/main
set TMPDIR=%TEMP%\ugo-install

echo ==============================================
echo  Ugo - installazione automatica (Windows)
echo ==============================================

rem --- 1. trova un Python funzionante (3.12 preferito) ------------------------
set PY=
for %%V in (312 311 310 313 314) do if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
for %%V in (312 311 310 313 314) do if not defined PY if exist "C:\Python%%V\python.exe" set "PY=C:\Python%%V\python.exe"
if defined PY "%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 set PY=

rem --- winget, poi ricontrollo -------------------------------------------------
if not defined PY echo Python 3.10+ non trovato: provo con winget...
if not defined PY winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements >nul 2>nul
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY "%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 set PY=

rem --- ultimo ricorso: installer ufficiale python.org --------------------------
if not defined PY echo winget non disponibile: scarico l'installer ufficiale di Python...
if not defined PY if not exist "%TMPDIR%" mkdir "%TMPDIR%"
if not defined PY curl -fsSL -o "%TMPDIR%\python-312.exe" https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
if not defined PY if errorlevel 1 exit /b 1
if not defined PY "%TMPDIR%\python-312.exe" /quiet InstallAllUsers=0 PrependPath=1
if not defined PY set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" echo ERRORE: Python non disponibile, installalo da python.org & exit /b 1
echo     Python OK: %PY%

rem --- 2. scarica l'installer Python completo (sorgente, venv, setup, run) ----
if not exist "%TMPDIR%" mkdir "%TMPDIR%"
curl -fsSL -o "%TMPDIR%\install.py" %RAW%/boot/install.py
if errorlevel 1 echo ERRORE: download non riuscito (connessione?) & exit /b 1
echo     installer scaricato in %TMPDIR%

rem --- 3. via libera ----------------------------------------------------------
"%PY%" "%TMPDIR%\install.py"
exit /b %ERRORLEVEL%
