"""Verify the installed distribution from an isolated, unrelated directory."""
import subprocess
import sys
import tempfile

CHECK = r'''
from pathlib import Path
import importlib.metadata
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from pagewriter import __version__, core
from pagewriter.resources import resource_path
app = QApplication([])
assert importlib.metadata.version("maliang") == __version__
assert not QIcon(str(resource_path("maliang.ico"))).isNull()
samples = list(resource_path("samples").glob("*.pagewriter"))
assert len(samples) == 3, samples
for path in samples:
    project = core.load_project(path)
    assert project.blocks
    assert not core.preflight(project), path
print("Installed package, icon and all three example projects verified.")
'''

with tempfile.TemporaryDirectory(prefix="maliang-install-") as folder:
    subprocess.run([sys.executable, "-I", "-c", CHECK], cwd=folder, check=True, timeout=60)
    subprocess.run([sys.executable, "-I", "-m", "pagewriter", "--smoke"],
                   cwd=folder, check=True, timeout=30)
