"""Actual document commands and native preview; no visible UI or hardware."""
import copy
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

if os.name == "nt":
    os.environ["QT_QPA_PLATFORM"] = "windows"
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from pagewriter import core
from pagewriter.document_service import DocumentService, fingerprint
from pagewriter.rendering import render_page
from pagewriter.service_errors import ServiceError

app = QApplication.instance() or QApplication([])


def runs(text="中文 Hello", size=12, font="maoken", underline=False):
    return [{"text": text, "font_id": font, "size_pt": size, "underline": underline}]


class _Window:
    """Minimal native-editor ownership contract, without opening a window."""
    def __init__(self):
        self.project = core.blank_project(100, 100)
        self.recorded, self.refreshed = 0, 0

    def record(self):
        self.recorded += 1

    def refresh(self):
        self.refreshed += 1


class DocumentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        self.changes = []
        self.doc = DocumentService(core.blank_project(100.25, 80.75),
                                   workspace=self.workspace, on_change=self.changes.append)

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, *operations):
        return self.doc.apply(list(operations), self.doc.revision)

    def text(self, **properties):
        return {"op": "add_text", "id": "text-a", "x_mm": 10, "y_mm": 10,
                "width_mm": 70, "runs": runs(), **properties}

    def image(self):
        path = self.workspace / "source.png"
        image = QImage(240, 120, QImage.Format.Format_RGB32)
        image.fill(QColor("#fdc098"))
        painter = QPainter(image)
        painter.fillRect(120, 0, 120, 120, QColor("#6688aa"))
        painter.end()
        self.assertTrue(image.save(str(path)))
        return path

    def test_structured_unicode_spaces_newlines_sizes_and_underline_roundtrip(self):
        first = "\n\n  中文  A 😀\nline\tZ\n"
        second = "  tail  "
        self.apply(self.text(runs=[*runs(first, 21, underline=True),
                                   *runs(second, 9, font="ems-readability")]))
        block = self.doc.get()["blocks"][0]
        self.assertEqual(block["text"], first + second)
        actual = [run for run in block["runs"] if run["text"].strip()]
        self.assertTrue(any(run["font_id"] == "maoken" and run["size_pt"] == 21
                            and run["underline"] for run in actual))
        self.assertTrue(any(run["font_id"] == "ems-readability" and run["size_pt"] == 9
                            and not run["underline"] for run in actual))
        self.assertEqual(block["writing_mode"], "single_line")
        # A second get must not turn lazy font/layout hydration into an edit.
        revision = self.doc.revision
        self.assertEqual(self.doc.get()["revision"], revision)

    def test_columns_separators_rotation_and_zone_avoidance_use_core_layout(self):
        self.apply(self.text(runs=runs("中文 Hello world " * 16, 10), columns=2,
            column_height_mm=25, column_gap_mm=4, column_separators=True,
            rotation_degrees=15, avoid_zones=False))
        block = self.doc.project.blocks[0]
        self.assertFalse(core.local_block_path(block).isEmpty())
        with_separators = core.path_to_data(core.composed_local_path(block))
        source = self.doc.get()["blocks"][0]
        self.assertEqual((source["columns"], source["column_height_mm"], source["column_gap_mm"]), (2, 25, 4))
        self.assertEqual(source["rotation_degrees"], 15)
        self.assertFalse(source["avoid_zones"])
        self.apply({"op": "update_text", "id": "text-a", "column_separators": False})
        without = core.path_to_data(core.composed_local_path(self.doc.project.blocks[0]))
        self.assertTrue(with_separators != without, "column separator must contribute writable geometry")

    def test_invalid_second_operation_rolls_back_everything(self):
        original = fingerprint(self.doc.project)
        revision = self.doc.revision
        with self.assertRaises((ServiceError, ValueError)):
            self.doc.apply([self.text(), {"op": "add_zone", "id": "bad-zone",
                "kind": "obstacle", "points_mm": [[1, 1], [2, 2]]}], revision)
        self.assertEqual(fingerprint(self.doc.project), original)
        self.assertEqual(self.doc.revision, revision)
        self.assertEqual(self.doc.undo_stack, [])
        self.assertEqual(self.changes, [])

    def test_revision_conflict_detects_editor_changes_and_ignores_layout_cache(self):
        window = _Window()
        self.doc = DocumentService(window=window, workspace=self.workspace)
        self.apply(self.text())
        self.assertEqual((window.recorded, window.refreshed), (1, 1))
        revision = self.doc.revision
        core.local_block_path(self.doc.project.blocks[0])
        self.assertEqual(self.doc.observe(), revision)
        window.project.blocks[0].x += 1  # Native UI changed the same model.
        with self.assertRaises(ServiceError) as caught:
            self.doc.apply([{"op": "remove", "id": "text-a"}], revision)
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")
        self.assertEqual(caught.exception.details["revision"], revision + 1)
        self.assertEqual(len(window.project.blocks), 1)
        self.assertEqual(self.doc.get()["revision"], revision + 1)

    def test_undo_redo_enforce_revision_and_restore_text(self):
        self.apply(self.text(runs=runs("before")))
        self.apply({"op": "update_text", "id": "text-a", "runs": runs("after")})
        revision = self.doc.revision
        self.assertEqual(self.doc.history(expected_revision=revision)["blocks"][0]["text"], "before")
        with self.assertRaises(ServiceError) as caught:
            self.doc.history(redo=True, expected_revision=revision)
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")
        self.assertEqual(self.doc.history(redo=True, expected_revision=self.doc.revision)["blocks"][0]["text"], "after")

    def test_workspace_rejects_traversal_and_real_symlink_escape(self):
        for path in ("../outside.pagewriter", str(self.workspace.parent / "outside.pagewriter")):
            with self.subTest(path=path), self.assertRaises(ServiceError) as caught:
                self.doc.path(path)
            self.assertEqual(caught.exception.code, "PATH_OUTSIDE_WORKSPACE")
        with tempfile.TemporaryDirectory() as outside:
            link = self.workspace / "external"
            junction = False
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                if os.name != "nt":
                    self.skipTest(f"real symlinks unavailable: {exc}")
                # Windows directory junctions need no symlink privilege and
                # exercise the same real filesystem containment boundary.
                result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(Path(outside).resolve())],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    self.skipTest("real symlinks and directory junctions unavailable")
                junction = True
            try:
                with self.assertRaises(ServiceError) as caught:
                    self.doc.path("external/escaped.png")
                self.assertEqual(caught.exception.code, "PATH_OUTSIDE_WORKSPACE")
            finally:
                if junction:
                    link.rmdir()
                else:
                    link.unlink()

    def test_photo_source_geometry_curves_preview_mapping_and_rotation(self):
        quad = [[10, 10], [230, 10], [230, 110], [10, 110]]
        edges = {"top": [[120, 6]], "bottom": [[120, 114]]}
        guides = [[[20, 60], [120, 64], [220, 60]]]
        self.apply({"op": "import_photo", "path": str(self.image()), "quad": quad,
                    "edges": edges, "guides": guides, "opacity": .8, "offset_mm": [1, 2]})
        photo = self.doc.get()["photo"]
        self.assertEqual((photo["quad"], photo["edges"], photo["guides"]), (quad, edges, guides))
        output = self.workspace / "preview.png"
        mapping = render_page(self.doc.project, output, scale=1.75)
        image = QImage(str(output))
        self.assertFalse(image.isNull())
        self.assertEqual(image.width(), math.ceil(100.25 * 1.75))
        self.assertEqual(image.height(), math.ceil(80.75 * 1.75))
        self.assertEqual((mapping["width_px"], mapping["height_px"]), (image.width(), image.height()))
        self.assertAlmostEqual(mapping["pixel_to_page_mm"][0] * image.width(), 100.25)
        self.assertAlmostEqual(mapping["page_to_pixel"][3] * 80.75, image.height())
        self.assertNotEqual(image.pixelColor(image.width() // 2, image.height() // 2), QColor("white"))
        self.apply({"op": "photo", "rotation_degrees": 90})
        rotated = core.data_to_image(self.doc.project.image_data)
        self.assertEqual((rotated.width(), rotated.height()), (120, 240))
        photo = self.doc.get()["photo"]
        self.assertIsNone(photo["quad"])
        self.assertIsNone(photo["edges"])
        self.assertIsNone(photo["guides"])
        self.assertEqual(photo["offset_mm"], [0, 0])
        self.assertEqual(photo["opacity"], .8)
        self.assertTrue(all(self.changes))

    def test_bad_photo_quad_and_rotation_are_atomic(self):
        self.apply({"op": "import_photo", "path": str(self.image())})
        baseline = fingerprint(self.doc.project)
        for properties in ({"quad": [[0, 0], [500, 0], [500, 110], [0, 110]]},
                           {"quad": [[0, 0], [230, 110], [230, 0], [0, 110]]},
                           {"rotation_degrees": 45}):
            with self.subTest(properties=properties), self.assertRaises((ServiceError, ValueError)):
                self.apply({"op": "photo", **properties})
            self.assertEqual(fingerprint(self.doc.project), baseline)

    def test_save_open_and_svg_export_keep_workspace_and_geometry(self):
        self.apply(self.text())
        revision = self.doc.revision
        saved = self.doc.save("safe.pagewriter")
        self.assertEqual(saved["path"], str(self.workspace / "safe.pagewriter"))
        self.assertEqual(self.doc.revision, revision)
        source = self.doc.get()["blocks"]
        self.doc.new(120, 130, expected_revision=revision)
        loaded = self.doc.open("safe.pagewriter", expected_revision=self.doc.revision)
        self.assertEqual(loaded["blocks"], source)
        target = self.doc.path("safe.svg")
        target.write_text(core.export_svg(self.doc.project), encoding="utf-8")
        self.assertIn('viewBox="0 0 100.25 80.75"', target.read_text(encoding="utf-8"))
        self.assertIn("<path", target.read_text(encoding="utf-8"))
        for action in (self.doc.save, self.doc.open, self.doc.path):
            with self.subTest(action=action.__name__), self.assertRaises(ServiceError) as caught:
                action("../escape.pagewriter")
            self.assertEqual(caught.exception.code, "PATH_OUTSIDE_WORKSPACE")


if __name__ == "__main__":
    unittest.main()
