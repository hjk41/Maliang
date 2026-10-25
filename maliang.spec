"""Build Maliang.exe with package resources; never install over a running copy."""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = Path(SPECPATH)
# Git/Poppler DLLs on PATH can otherwise contaminate the Qt dependency scan.
windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
python = Path(sys.executable).parent
base_python = Path(sys.base_prefix)
os.environ["PATH"] = os.pathsep.join(map(str, [python, python / "DLLs", base_python,
    base_python / "DLLs", windows / "System32", windows]))
a = Analysis([str(ROOT / "main.py"), str(ROOT / "agent_main.py")], pathex=[str(ROOT)],
             datas=collect_data_files("pagewriter") + collect_data_files("jsonschema_specifications")
                   + copy_metadata("mcp", recursive=True),
             hiddenimports=["serial.tools.list_ports"], excludes=["tkinter", "unittest", "PIL"])
a.binaries = [item for item in a.binaries
              if not Path(item[0]).name.lower().startswith(("icuuc", "icudt", "icuin"))]
pyz = PYZ(a.pure)
entrypoints = {str((ROOT / name).resolve()) for name in ("main.py", "agent_main.py")}
def scripts_for(name):
    selected = str((ROOT / name).resolve())
    return [item for item in a.scripts if str(Path(item[1]).resolve()) not in entrypoints
            or str(Path(item[1]).resolve()) == selected]

exe = EXE(pyz, scripts_for("main.py"), [], exclude_binaries=True, name="Maliang", console=False,
          icon=str(ROOT / "pagewriter/resources/maliang.ico"), upx=False)
agent_exe = EXE(pyz, scripts_for("agent_main.py"), [], exclude_binaries=True,
          name="MaliangAgent", console=True, icon=str(ROOT / "pagewriter/resources/maliang.ico"), upx=False)
coll = COLLECT(exe, agent_exe, a.binaries, a.datas, name="Maliang", upx=False)
