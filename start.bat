@echo off
rem Startet das Labeltool. Beim ersten Start wird eine venv angelegt.
cd /d "%~dp0"

if not exist config.yaml (
  copy config.example.yaml config.yaml >nul
  echo config.yaml wurde angelegt - bitte Part-DB-URL und Token eintragen.
  notepad config.yaml
  pause
)

rem Marker-Datei: Setup gilt erst als fertig, wenn alle Pakete installiert sind.
if not exist .venv\.setup-ok (
  echo Richte Python-Umgebung ein ...
  if exist .venv rmdir /s /q .venv

  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
  if not exist .venv\Scripts\python.exe (
    echo Fehler: venv konnte nicht angelegt werden. Ist Python 3 installiert?
    pause
    exit /b 1
  )

  .venv\Scripts\python -m pip install --upgrade pip || goto :setupfail
  .venv\Scripts\python -m pip install -r requirements.txt || goto :setupfail
  copy /y requirements.txt .venv\requirements.installed.txt >nul
  echo ok> .venv\.setup-ok
)

rem requirements.txt seit der Einrichtung geaendert (z. B. nach einem Update)? Dann Pakete nachinstallieren.
fc /b requirements.txt .venv\requirements.installed.txt >nul 2>&1
if errorlevel 1 (
  echo Pakete werden aktualisiert ...
  .venv\Scripts\python -m pip install -r requirements.txt || goto :setupfail
  copy /y requirements.txt .venv\requirements.installed.txt >nul
)

.venv\Scripts\python -m labeltool config.yaml
pause
exit /b

:setupfail
echo Fehler bei der Paketinstallation - beim naechsten Start wird es erneut versucht.
pause
exit /b 1
