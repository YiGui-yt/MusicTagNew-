from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QApplication

import app_qt
from app_qt import CombinedTagSearchDialog, MusicTagApp, OverwriteOptionsDialog
from core import SearchHit, Track


class CombinedTagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_search_populates_candidate_table(self) -> None:
        def fake_search(query, _sources, limit_per_source=8, logger=None):
            return [
                SearchHit(
                    "QQ音乐",
                    "abc",
                    "晴天",
                    "周杰伦",
                    "叶惠美",
                    269.0,
                    year="2003",
                    genre="流行",
                    track="3",
                    disc="1",
                    cover_url="https://example.invalid/cover.jpg",
                )
            ], []

        original = app_qt.search_sources
        app_qt.search_sources = fake_search
        window = MusicTagApp()
        try:
            track = Track(Path("sample.flac"))
            track.tags.update({"title": "晴天", "artist": "周杰伦", "album": "叶惠美"})
            dialog = CombinedTagSearchDialog(window, track, "晴天 周杰伦", ["QQ音乐"])
            dialog.search()
            self.assertTrue(self.wait_until(lambda: dialog.has_searched))
            self.assertEqual(1, dialog.results_table.rowCount())
            self.assertEqual("晴天", dialog.results_table.item(0, 1).text())
            self.assertEqual("流行", dialog.results_table.item(0, 6).text())
            self.assertTrue(dialog.refresh_button.isEnabled())
            dialog.close()
        finally:
            app_qt.search_sources = original
            window.close()
            self.app.processEvents()

    def test_apply_only_selected_fields(self) -> None:
        window = MusicTagApp()
        try:
            track = Track(Path("sample.flac"))
            track.tags.update(
                {
                    "title": "旧标题",
                    "artist": "旧艺术家",
                    "album": "旧专辑",
                }
            )
            window.tracks = [track]
            window.track_by_path = {str(track.path): track}
            window.refresh_tracks()
            window.table.selectRow(0)
            self.app.processEvents()
            window.field_edits["title"].setText("新标题")
            window.apply_combined_tag(
                {
                    "title": "候选标题",
                    "artist": "候选艺术家",
                    "album": "候选专辑",
                    "year": "2026",
                    "track": "2",
                    "disc": "1",
                    "genre": "Pop",
                    "comment": "candidate",
                    "lyrics": "[00:00.00]歌词",
                },
                {
                    "title": True,
                    "artist": False,
                    "album": True,
                    "year": True,
                    "track": True,
                    "disc": True,
                    "genre": True,
                    "comment": False,
                    "lyrics": True,
                    "cover": False,
                },
                None,
                None,
            )
            self.assertEqual("候选标题", window.field_edits["title"].text())
            self.assertEqual("旧艺术家", window.field_edits["artist"].text())
            self.assertEqual("候选专辑", window.field_edits["album"].text())
            self.assertEqual("", window.field_edits["comment"].text())
            self.assertIn("歌词", window.lyrics_edit.toPlainText())
            self.assertTrue(window.editor_dirty)
        finally:
            window.editor_dirty = False
            for track in window.tracks:
                track.dirty = False
            window.close()
            self.app.processEvents()

    def test_overwrite_options_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_path = OverwriteOptionsDialog.SETTINGS_PATH
            OverwriteOptionsDialog.SETTINGS_PATH = Path(directory) / "options.json"
            try:
                values = OverwriteOptionsDialog.load_saved()
                self.assertTrue(values["title"])
                self.assertFalse(values["comment"])
                values["title"] = False
                values["comment"] = True
                OverwriteOptionsDialog.save_values(values)
                loaded = OverwriteOptionsDialog.load_saved()
                self.assertFalse(loaded["title"])
                self.assertTrue(loaded["comment"])
            finally:
                OverwriteOptionsDialog.SETTINGS_PATH = original_path


if __name__ == "__main__":
    unittest.main(verbosity=2)
