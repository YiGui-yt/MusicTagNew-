from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import (
    SearchHit,
    Track,
    best_hit,
    format_http_status_line,
    load_track,
    merge_translation,
    parse_netease_search,
    parse_qq_search,
    read_text_file,
    save_track_tags,
)


class CoreTests(unittest.TestCase):
    def test_http_status_line_format(self) -> None:
        self.assertEqual("HTTP/1.1 200 OK", format_http_status_line(11, 200, "OK"))

    def test_best_hit_prefers_title_and_artist(self) -> None:
        hits = [
            SearchHit("网易云", "1", "晴天", "周杰伦", "叶惠美"),
            SearchHit("网易云", "2", "晴天", "其他歌手", "其他专辑"),
        ]
        selected = best_hit(hits, "晴天", "周杰伦", "叶惠美")
        self.assertIsNotNone(selected)
        self.assertEqual("1", selected.track_id)
        self.assertGreater(selected.score, hits[1].score)

    def test_merge_translation_matches_timestamp(self) -> None:
        original = "[00:01.00]Hello\n[00:02.50]World"
        translated = "[00:01.00]你好\n[00:02.50]世界"
        merged = merge_translation(original, translated)
        self.assertIn("[00:01.00]Hello\n[00:01.00]你好", merged)
        self.assertIn("[00:02.50]World\n[00:02.50]世界", merged)

    def test_netease_search_parser(self) -> None:
        payload = {
            "result": {
                "songs": [
                    {
                        "id": 123,
                        "name": "夜曲",
                        "duration": 226000,
                        "artists": [{"name": "周杰伦"}],
                        "album": {"name": "十一月的萧邦"},
                    }
                ]
            }
        }
        hits = parse_netease_search(payload)
        self.assertEqual(1, len(hits))
        self.assertEqual("123", hits[0].track_id)
        self.assertEqual("周杰伦", hits[0].artist)
        self.assertAlmostEqual(226.0, hits[0].duration)

    def test_qq_search_parser(self) -> None:
        payload = {
            "req_0": {
                "data": {
                    "body": {
                        "song": {
                            "list": [
                                {
                                    "id": 123,
                                    "mid": "abc",
                                    "title": "稻香",
                                    "interval": 223,
                                    "singer": [{"name": "周杰伦"}],
                                    "album": {"name": "魔杰座"},
                                }
                            ]
                        }
                    }
                }
            }
        }
        hits = parse_qq_search(payload)
        self.assertEqual(1, len(hits))
        self.assertEqual("abc", hits[0].track_id)
        self.assertEqual("123", hits[0].numeric_id)
        self.assertEqual("魔杰座", hits[0].album)

    def test_text_file_falls_back_to_gb18030(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lyrics.lrc"
            path.write_bytes("[00:00.00]测试".encode("gb18030"))
            self.assertIn("测试", read_text_file(path))

    def test_mp3_tag_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roundtrip.mp3"
            path.write_bytes(b"")
            track = Track(path=path)
            track.tags.update(
                {
                    "title": "测试标题",
                    "artist": "测试歌手",
                    "album": "测试专辑",
                    "year": "2026",
                    "track": "2/10",
                    "disc": "1/1",
                    "genre": "Pop",
                    "album_artist": "专辑歌手",
                    "composer": "作曲",
                    "lyricist": "作词",
                    "comment": "备注",
                }
            )
            track.lyrics = "[00:00.00]测试歌词"
            track.cover = b"fake-jpeg"
            track.cover_mime = "image/jpeg"
            logs: list[str] = []
            save_track_tags(track, logs.append)

            loaded = load_track(path, logs.append)
            self.assertIsNone(loaded.load_error)
            self.assertEqual("测试标题", loaded.tags["title"])
            self.assertEqual("测试歌手", loaded.tags["artist"])
            self.assertEqual("[00:00.00]测试歌词", loaded.lyrics)
            self.assertEqual(b"fake-jpeg", loaded.cover)
            self.assertTrue(any(line.startswith("[保存] 写入标签:") for line in logs))
            self.assertTrue(any(line.startswith("[读取] 标签:") for line in logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
