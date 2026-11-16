from __future__ import annotations

import os
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app_qt
from app_qt import LyricSearchDialog, MusicTagApp
from core import SearchHit, Track


class UiTests(unittest.TestCase):
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

    def test_refresh_button_resends_request(self) -> None:
        calls: list[str] = []

        def fake_search(query, _sources, limit_per_source=8, logger=None):
            calls.append(query)
            time.sleep(0.05)
            return [SearchHit("QQ音乐", "abc", "晴天", "周杰伦", "叶惠美", 269.0)], []

        original = app_qt.search_sources
        app_qt.search_sources = fake_search
        window = MusicTagApp()
        try:
            track = Track(Path("sample.flac"))
            track.tags.update({"title": "晴天", "artist": "周杰伦", "album": "叶惠美"})
            dialog = LyricSearchDialog(window, track, "晴天 周杰伦", ["QQ音乐"])
            self.assertFalse(dialog.refresh_button.isEnabled())

            dialog.search()
            self.assertTrue(self.wait_until(lambda: dialog.has_searched))
            self.assertTrue(dialog.refresh_button.isEnabled())
            self.assertEqual(1, len(calls))

            dialog.refresh_results()
            self.assertTrue(
                self.wait_until(lambda: len(calls) == 2 and dialog.refresh_button.isEnabled())
            )
            dialog.close()
        finally:
            app_qt.search_sources = original
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
