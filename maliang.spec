"""Build Maliang.exe with package resources; never install over a running copy."""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)
# Git/Poppler DLLs on PATH can otherwise contaminate the Qt dependency scan.
windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
python = Path(sys.executable).parent
os.environ["PATH"] = os.pathsep.join(map(str, [python, python / "DLLs", windows / "System32", windows]))
a = Analysis([str(ROOT / "main.py")], pathex=[str(ROOT)],
             datas=collect_data_files("pagewriter"),
             hiddenimports=["serial.tools.list_ports"], excludes=["tkinter", "unittest", "PIL"])
a.binaries = [item for item in a.binaries
              if not Path(item[0]).name.lower().startswith(("icuuc", "icudt", "icuin"))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Maliang", console=False,
          icon=str(ROOT / "pagewriter/resources/maliang.ico"), upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Maliang", upx=False)
