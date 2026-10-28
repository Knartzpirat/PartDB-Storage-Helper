# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-Bauplan für den Windows-Druck-Helfer („onedir“, ein gemeinsamer Ordner):
#   LabeltoolHelfer.exe       Helfer ohne Konsolenfenster, Protokoll in %LOCALAPPDATA%\Labeltool-Helfer\helfer.log
#   LabeltoolHelferSetup.exe  Einrichtungs-Assistent (tkinter)
# Bauen: installer\build.ps1 (bzw. pyinstaller --noconfirm --distpath installer\build\bundle
#        --workpath installer\build\work installer\helfer.spec)
import os

PROJEKT = os.path.abspath(os.path.join(SPECPATH, ".."))

# pywin32 (b-PAC über COM, Druckerliste), websockets (Verbindung zum Server), pyyaml, tkinter
VERSTECKT = ["pythoncom", "pywintypes", "win32com", "win32com.client", "win32print", "win32api",
             "win32timezone", "websockets.asyncio.client", "yaml"]
# Server-Pakete gehören nicht in den Helfer
OHNE = ["fastapi", "uvicorn", "starlette", "httpx", "pydantic", "pytest", "_pytest", "pip",
        "setuptools", "pkg_resources", "IPython", "numpy"]


def analyse(skript, tk):
    return Analysis([os.path.join(SPECPATH, skript)], pathex=[PROJEKT], binaries=[], datas=[],
                    hiddenimports=VERSTECKT + (["tkinter", "tkinter.ttk", "tkinter.filedialog",
                                                "tkinter.messagebox"] if tk else []),
                    hookspath=[], runtime_hooks=[], excludes=OHNE, noarchive=False, optimize=0)


a_helfer = analyse("start_helfer.py", tk=True)   # tkinter auch hier: Diagnose prüft das Bundle
a_setup = analyse("start_setup.py", tk=True)

pyz_helfer = PYZ(a_helfer.pure)
pyz_setup = PYZ(a_setup.pure)

exe_helfer = EXE(pyz_helfer, a_helfer.scripts, [], exclude_binaries=True, name="LabeltoolHelfer",
                 debug=False, strip=False, upx=False, console=False,
                 version=os.path.join(SPECPATH, "build", "version_helfer.txt")
                 if os.path.isfile(os.path.join(SPECPATH, "build", "version_helfer.txt")) else None)
exe_setup = EXE(pyz_setup, a_setup.scripts, [], exclude_binaries=True, name="LabeltoolHelferSetup",
                debug=False, strip=False, upx=False, console=False,
                version=os.path.join(SPECPATH, "build", "version_setup.txt")
                if os.path.isfile(os.path.join(SPECPATH, "build", "version_setup.txt")) else None)

coll = COLLECT(exe_helfer, a_helfer.binaries, a_helfer.datas,
               exe_setup, a_setup.binaries, a_setup.datas,
               strip=False, upx=False, name="Labeltool-Helfer")
