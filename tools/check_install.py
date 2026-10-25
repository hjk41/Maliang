"""Verify the installed distribution from an isolated, unrelated directory."""
import subprocess
import sys
import tempfile

CHECK = r'''
from pathlib import Path
import importlib.metadata
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from pagewriter import __version__, core, stroke_fonts
from pagewriter.resources import resource_path
app = QApplication([])
assert importlib.metadata.version("maliang") == __version__
installed = Path(importlib.metadata.distribution("maliang").locate_file("pagewriter")).resolve()
assert Path(core.__file__).resolve().parent == installed, "Checkout shadowed installed package"
assert not QIcon(str(resource_path("maliang.ico"))).isNull()
samples = list(resource_path("samples").glob("*.pagewriter"))
assert len(samples) == 3, samples
for path in samples:
    project = core.load_project(path)
    assert project.blocks
    assert not core.preflight(project), path
fonts = stroke_fonts.descriptors()
assert len(fonts) == 9
for font in fonts:
    block = core.TextBlock(html='<p>中文 Hello，123</p>', writing_mode='single_line',
        stroke_font=font['family'], stroke_fonts={font['family']:font})
    assert core.local_block_path(block).elementCount() > 10
    assert not any(row['missing_glyphs'] for row in block.fonts)
    assert not core.font_warnings(core.Project(blocks=[block]))
print("Installed package, nine offline fonts, icon and all three example projects verified.")
'''

with tempfile.TemporaryDirectory(prefix="maliang-install-") as folder:
    subprocess.run([sys.executable, "-I", "-c", CHECK], cwd=folder, check=True, timeout=60)
    subprocess.run([sys.executable, "-I", "-m", "pagewriter", "--smoke"],
                   cwd=folder, check=True, timeout=30)
