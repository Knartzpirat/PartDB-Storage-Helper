@echo off
rem Startet den Druck-Helfer (verbindet diesen PC mit dem Labeltool-Server, z. B. im Docker auf Unraid).
rem Beim ersten Start wird eine venv angelegt. Fehlt helfer.yaml oder heisst der Aufruf
rem "helfer.bat einrichten", oeffnet sich zuerst der Einrichtungs-Assistent.
setlocal
cd /d "%~dp0"

rem Paketliste: im Helfer-Download requirements-helfer.txt, im kompletten Tool requirements.txt
set "REQ=requirements.txt"
if exist requirements-helfer.txt set "REQ=requirements-helfer.txt"

rem Marker-Datei: Setup gilt erst als fertig, wenn alle Pakete installiert sind.
if not exist .venv\.setup-ok (
  echo Richte Python-Umgebung ein ^(einmalig, dauert ein bis zwei Minuten^) ...
  if exist .venv rmdir /s /q .venv

  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
  if not exist .venv\Scripts\python.exe (
    echo Fehler: venv konnte nicht angelegt werden. Ist Python 3 installiert? https://www.python.org/downloads/
    pause
    exit /b 1
  )

  .venv\Scripts\python -m pip install --upgrade pip || goto :setupfail
  .venv\Scripts\python -m pip install -r "%REQ%" || goto :setupfail
  copy /y "%REQ%" .venv\requirements.installed.txt >nul
  echo ok> .venv\.setup-ok
)

rem Paketliste seit der Einrichtung geaendert (z. B. nach einem Update)? Dann Pakete nachinstallieren.
fc /b "%REQ%" .venv\requirements.installed.txt >nul 2>&1
if errorlevel 1 (
  echo Pakete werden aktualisiert ...
  .venv\Scripts\python -m pip install -r "%REQ%" || goto :setupfail
  copy /y "%REQ%" .venv\requirements.installed.txt >nul
)

set "EINRICHTEN="
if /i "%~1"=="einrichten" set "EINRICHTEN=1"
if not exist helfer.yaml set "EINRICHTEN=1"
if defined EINRICHTEN (
  echo Einrichtungs-Assistent wird geoeffnet ...
  .venv\Scripts\python -m labeltool.helfer_setup --aus-bat helfer.yaml
  if errorlevel 1 (
    echo Einrichtung abgebrochen - helfer.yaml wurde nicht geaendert.
    pause
    exit /b 1
  )
)

title Labeltool Druck-Helfer
.venv\Scripts\python -m labeltool.helfer helfer.yaml
echo.
echo Der Druck-Helfer wurde beendet.
pause
exit /b

:setupfail
echo Fehler bei der Paketinstallation - beim naechsten Start wird es erneut versucht.
pause
exit /b 1
