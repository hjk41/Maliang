"""Application entry point, shared by source, installed wheel and frozen EXE."""
import sys
import argparse
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import __version__
from .resources import resource_path
from .ui import MainWindow


def main(argv=None):
    parser = argparse.ArgumentParser(prog='Maliang')
    parser.add_argument('project', nargs='?', type=Path)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--agent-api', action='store_true', help='Enable authenticated local agent interface')
    parser.add_argument('--agent-workspace', type=Path)
    parser.add_argument('--agent-connection', type=Path)
    parser.add_argument('--device-profile', action='append', type=Path, default=[])
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if options.device_profile:
        from .devices import load_profile
        for path in options.device_profile:
            load_profile(path)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hjk41.Maliang.Desktop")
    app = QApplication(sys.argv)
    app.setApplicationName("马良")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Maliang")
    app.setWindowIcon(QIcon(str(resource_path("maliang.ico"))))
    window = MainWindow(blank=True)
    if options.project:
        window.open_path(options.project)
    if options.agent_workspace:
        window._agent_workspace = options.agent_workspace.resolve()
    if options.agent_api:
        from .agent_ui import start_api
        start_api(window, options.agent_connection)
    window.show()
    if options.smoke:
        # Exercise packaged SQLite paths and private layout fonts as well as
        # the blank window. This never creates a machine job or opens a port.
        from . import core, stroke_fonts
        fonts = stroke_fonts.descriptors()
        if len(fonts) != 9:
            raise RuntimeError('Nine bundled fonts are required for the smoke check')
        for font in fonts:
            block = core.TextBlock(html='<p>中文 Hello，123</p>', writing_mode='single_line',
                stroke_font=font['family'], stroke_fonts={font['family']:font})
            if core.local_block_path(block).isEmpty() or any(item['missing_glyphs'] for item in block.fonts):
                raise RuntimeError('Bundled font smoke check failed: '+font['name'])
        from PySide6.QtCore import QTimer
        QTimer.singleShot(800, app.quit)
    return app.exec()
