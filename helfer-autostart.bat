@echo off
rem Legt eine Verknuepfung "Labeltool Druck-Helfer" im Autostart-Ordner des aktuellen Benutzers an
rem (shell:startup), die helfer.bat minimiert startet. Entfernen: dieselbe Datei mit "entfernen" aufrufen
rem oder die Verknuepfung im Autostart-Ordner loeschen.
setlocal
cd /d "%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Labeltool Druck-Helfer.lnk"

if /i "%~1"=="entfernen" (
  if exist "%LNK%" del "%LNK%"
  echo Autostart-Verknuepfung entfernt.
  pause
  exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);" ^
  "$s.TargetPath = '%~dp0helfer.bat';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.WindowStyle = 7;" ^
  "$s.Description = 'Labeltool Druck-Helfer';" ^
  "$s.Save()"
if errorlevel 1 (
  echo Fehler: Verknuepfung konnte nicht angelegt werden.
) else (
  echo Autostart eingerichtet: "%LNK%"
  echo Der Druck-Helfer startet ab der naechsten Anmeldung minimiert.
)
pause
