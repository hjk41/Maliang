"""Application entry point, shared by source, installed wheel and frozen EXE."""
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import __version__
from .resources import resource_path
from .ui import MainWindow


def main():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hjk41.Maliang.Desktop")
    app = QApplication(sys.argv)
    app.setApplicationName("马良")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Maliang")
    app.setWindowIcon(QIcon(str(resource_path("maliang.ico"))))
    window = MainWindow(blank=True)
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        window.open_path(Path(sys.argv[1]))
    window.show()
    if "--smoke" in sys.argv:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(800, app.quit)
    return app.exec()
