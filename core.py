from __future__ import annotations

import base64
import html
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable

from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.asf import ASF, ASFUnicodeAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    ID3NoHeaderError,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TEXT,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    USLT,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE


SUPPORTED_EXTENSIONS = (
    ".mp3",
    ".flac",
    ".m4a",
    ".mp4",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".aif",
    ".aiff",
    ".wma",
    ".asf",
)

FIELD_LABELS = (
    ("title", "标题"),
    ("artist", "艺术家"),
    ("album", "专辑"),
    ("year", "年份"),
    ("track", "音轨号"),
    ("disc", "碟号"),
    ("genre", "风格"),
    ("album_artist", "专辑艺术家"),
    ("composer", "作曲家"),
    ("lyricist", "作词家"),
    ("comment", "注释"),
)

_TIMESTAMP_RE = re.compile(r"^\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_NUMBER_RE = re.compile(r"(\d+)(?:\s*/\s*(\d+))?")
_LOG_VALUE_LIMIT = 160
_LOG_BODY_LIMIT = 800

LogFn = Callable[[str], None]


class TagWriteError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


def _log(logger: LogFn | None, message: str) -> None:
    if logger is None:
        return
    try:
        logger(message)
    except Exception:
        pass


def _shorten(value: object, limit: int = _LOG_VALUE_LIMIT) -> str:
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + f"... ({len(text)} chars)"


def _tag_summary(track: "Track") -> str:
    values = []
    for key, label in FIELD_LABELS:
        value = track.tags.get(key, "").strip()
        if value:
            values.append(f"{label}={_shorten(value)}")
    return " | ".join(values) if values else "(空)"


def _lyrics_summary(lyrics: str) -> str:
    text = lyrics or ""
    return f"{len(text)} 字符 | {_shorten(text)}"


def format_http_status_line(version: int | None, status: int | object, reason: str | object) -> str:
    version_value = int(version or 11)
    if version_value >= 20:
        protocol = "HTTP/2"
    else:
        protocol = f"HTTP/{version_value // 10}.{version_value % 10}"
    return f"{protocol} {status} {reason}".strip()


def _log_request(logger: LogFn | None, request: urllib.request.Request, body_text: str = "") -> None:
    _log(logger, f"[HTTP] -> {request.get_method()} {request.full_url}")
    for name, value in request.header_items():
        lower_name = name.lower()
        display_value = "<redacted>" if lower_name in {"cookie", "authorization"} else value
        _log(logger, f"[HTTP] -> {name}: {display_value}")
    if body_text:
        _log(logger, f"[HTTP] -> body: {_shorten(body_text, _LOG_BODY_LIMIT)}")


def _log_http_response(logger: LogFn | None, response, raw: bytes) -> None:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", "")
    reason = getattr(response, "reason", "")
    version = getattr(response, "version", None)
    status_line = format_http_status_line(version, status, reason)
    _log(logger, f"[HTTP] <- {status_line}")


def _read_url_response(
    request: urllib.request.Request,
    timeout: float,
    logger: LogFn | None,
) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            _log_http_response(logger, response, raw)
    except urllib.error.HTTPError as exc:
        try:
            raw_error = exc.read(8 * 1024 * 1024 + 1)
        except Exception:
            raw_error = b""
        _log_http_response(logger, exc, raw_error)
        raise ProviderError(f"HTTP {exc.code} {exc.reason}") from exc
    except Exception as exc:
        _log(logger, f"[HTTP] !! {type(exc).__name__}: {exc}")
        raise ProviderError(str(exc)) from exc
    if len(raw) > 8 * 1024 * 1024:
        _log(logger, "[HTTP] !! response exceeded 8 MiB")
        raise ProviderError("响应过大")
    return raw
@dataclass(slots=True)
class Track:
    path: Path
    tags: dict[str, str] = field(default_factory=dict)
    lyrics: str = ""
    cover: bytes | None = None
    cover_mime: str | None = None
    duration: float | None = None
    load_error: str | None = None
    dirty: bool = False

    def snapshot(self) -> dict[str, str]:
        return {key: self.tags.get(key, "") for key, _ in FIELD_LABELS}


@dataclass(slots=True)
class SearchHit:
    source: str
    track_id: str
    title: str
    artist: str
    album: str
    duration: float | None = None
    score: float = 0.0
    numeric_id: str = ""
    year: str = ""
    genre: str = ""
    track: str = ""
    disc: str = ""
    comment: str = ""
    cover_url: str = ""
    album_id: str = ""

    @property
    def caption(self) -> str:
        return f"{self.title} - {self.artist}"


def _first_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        return str(value[0])
    return str(value)


def _tag_text(tags: object, *keys: str) -> str:
    if tags is None:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except (AttributeError, KeyError, TypeError):
            value = None
        text = _first_text(value)
        if text:
            return text
    return ""


def _id3_text(tag: ID3, frame_id: str) -> str:
    values: list[str] = []
    for frame in tag.getall(frame_id):
        text = getattr(frame, "text", None)
        if text is None:
            values.append(str(frame))
        else:
            values.extend(str(value) for value in text if str(value))
    return "/".join(values)


def _read_id3(tag: ID3, track: Track) -> None:
    track.tags["title"] = _id3_text(tag, "TIT2")
    track.tags["artist"] = _id3_text(tag, "TPE1")
    track.tags["album"] = _id3_text(tag, "TALB")
    track.tags["year"] = _id3_text(tag, "TDRC") or _id3_text(tag, "TYER")
    track.tags["track"] = _id3_text(tag, "TRCK")
    track.tags["disc"] = _id3_text(tag, "TPOS")
    track.tags["genre"] = _id3_text(tag, "TCON")
    track.tags["album_artist"] = _id3_text(tag, "TPE2")
    track.tags["composer"] = _id3_text(tag, "TCOM")
    track.tags["lyricist"] = _id3_text(tag, "TEXT")

    comments = tag.getall("COMM")
    if comments:
        comments.sort(key=lambda frame: getattr(frame, "desc", "") != "")
        track.tags["comment"] = _first_text(getattr(comments[0], "text", ""))

    lyrics_frames = tag.getall("USLT")
    if lyrics_frames:
        track.lyrics = _first_text(getattr(lyrics_frames[0], "text", ""))

    pictures = tag.getall("APIC")
    if pictures:
        track.cover = bytes(pictures[0].data)
        track.cover_mime = pictures[0].mime or None


def _read_vorbis(tags: object, track: Track) -> None:
    track.tags["title"] = _tag_text(tags, "title")
    track.tags["artist"] = _tag_text(tags, "artist")
    track.tags["album"] = _tag_text(tags, "album")
    track.tags["year"] = _tag_text(tags, "date", "year")
    track.tags["track"] = _tag_text(tags, "tracknumber", "track")
    track.tags["disc"] = _tag_text(tags, "discnumber", "disc")
    track.tags["genre"] = _tag_text(tags, "genre")
    track.tags["album_artist"] = _tag_text(tags, "albumartist", "album artist")
    track.tags["composer"] = _tag_text(tags, "composer")
    track.tags["lyricist"] = _tag_text(tags, "lyricist")
    track.tags["comment"] = _tag_text(tags, "comment", "description")
    track.lyrics = _tag_text(tags, "lyrics", "unsyncedlyrics")


def _read_mp4(audio: MP4, track: Track) -> None:
    tags = audio.tags
    track.tags["title"] = _tag_text(tags, "\xa9nam")
    track.tags["artist"] = _tag_text(tags, "\xa9ART")
    track.tags["album"] = _tag_text(tags, "\xa9alb")
    track.tags["year"] = _tag_text(tags, "\xa9day")
    track.tags["genre"] = _tag_text(tags, "\xa9gen")
    track.tags["album_artist"] = _tag_text(tags, "aART")
    track.tags["composer"] = _tag_text(tags, "\xa9wrt")
    track.tags["comment"] = _tag_text(tags, "\xa9cmt")
    track.lyrics = _tag_text(tags, "\xa9lyr")

    raw_lyricist = _tag_text(tags, "----:com.apple.iTunes:LYRICIST")
    if raw_lyricist.startswith("b'"):
        track.tags["lyricist"] = ""
    else:
        track.tags["lyricist"] = raw_lyricist

    track_number = tags.get("trkn") if tags is not None else None
    if track_number:
        number, total = track_number[0]
        track.tags["track"] = f"{number}/{total}" if total else str(number)

    disc_number = tags.get("disk") if tags is not None else None
    if disc_number:
        number, total = disc_number[0]
        track.tags["disc"] = f"{number}/{total}" if total else str(number)

    covers = tags.get("covr") if tags is not None else None
    if covers:
        track.cover = bytes(covers[0])
        track.cover_mime = "image/png" if covers[0].imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"


def _read_asf(audio: ASF, track: Track) -> None:
    tags = audio.tags
    track.tags["title"] = _tag_text(tags, "Title")
    track.tags["artist"] = _tag_text(tags, "Author")
    track.tags["album"] = _tag_text(tags, "WM/AlbumTitle")
    track.tags["year"] = _tag_text(tags, "WM/Year")
    track.tags["track"] = _tag_text(tags, "WM/TrackNumber")
    track.tags["disc"] = _tag_text(tags, "WM/PartOfSet")
    track.tags["genre"] = _tag_text(tags, "WM/Genre")
    track.tags["album_artist"] = _tag_text(tags, "WM/AlbumArtist")
    track.tags["composer"] = _tag_text(tags, "WM/Composer")
    track.tags["lyricist"] = _tag_text(tags, "WM/Lyricist")
    track.tags["comment"] = _tag_text(tags, "Description", "Comment")
    track.lyrics = _tag_text(tags, "WM/Lyrics")


def _read_generic_easy(path: Path, track: Track) -> None:
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise TagWriteError("无法识别此音频格式")
    if audio.info is not None:
        track.duration = float(audio.info.length)
    tags = audio.tags
    track.tags["title"] = _tag_text(tags, "title")
    track.tags["artist"] = _tag_text(tags, "artist")
    track.tags["album"] = _tag_text(tags, "album")
    track.tags["year"] = _tag_text(tags, "date", "year")
    track.tags["track"] = _tag_text(tags, "tracknumber")
    track.tags["disc"] = _tag_text(tags, "discnumber")
    track.tags["genre"] = _tag_text(tags, "genre")
    track.tags["album_artist"] = _tag_text(tags, "albumartist")
    track.tags["composer"] = _tag_text(tags, "composer")
    track.tags["lyricist"] = _tag_text(tags, "lyricist")
    track.tags["comment"] = _tag_text(tags, "comment")
    track.lyrics = _tag_text(tags, "lyrics")


def load_track(path: str | Path, logger: LogFn | None = None) -> Track:
    file_path = Path(path)
    track = Track(path=file_path)
    suffix = file_path.suffix.lower()
    _log(logger, f"[读取] 打开: {file_path} ({suffix or 'unknown'})")
    try:
        if suffix == ".mp3":
            try:
                audio = MutagenFile(file_path)
            except Exception:
                audio = None
            if audio is not None and audio.info is not None:
                track.duration = float(audio.info.length)
            try:
                tag = ID3(file_path)
            except ID3NoHeaderError:
                tag = ID3()
            _read_id3(tag, track)
        elif suffix == ".flac":
            audio = FLAC(file_path)
            track.duration = float(audio.info.length)
            _read_vorbis(audio.tags, track)
            if audio.pictures:
                track.cover = audio.pictures[0].data
                track.cover_mime = audio.pictures[0].mime or None
        elif suffix in (".m4a", ".mp4"):
            audio = MP4(file_path)
            track.duration = float(audio.info.length)
            _read_mp4(audio, track)
        elif suffix in (".ogg", ".oga", ".opus"):
            audio = OggOpus(file_path) if suffix == ".opus" else OggVorbis(file_path)
            track.duration = float(audio.info.length)
            _read_vorbis(audio.tags, track)
            picture_data = _tag_text(audio.tags, "metadata_block_picture")
            if picture_data:
                try:
                    picture = Picture(base64.b64decode(picture_data))
                    track.cover = picture.data
                    track.cover_mime = picture.mime or None
                except Exception:
                    pass
        elif suffix in (".wma", ".asf"):
            audio = ASF(file_path)
            if audio.info is not None:
                track.duration = float(audio.info.length)
            _read_asf(audio, track)
        elif suffix in (".wav", ".aif", ".aiff"):
            audio = WAVE(file_path) if suffix == ".wav" else AIFF(file_path)
            if audio.info is not None:
                track.duration = float(audio.info.length)
            if isinstance(audio.tags, ID3):
                _read_id3(audio.tags, track)
            else:
                _read_generic_easy(file_path, track)
        else:
            _read_generic_easy(file_path, track)
    except Exception as exc:
        track.load_error = str(exc)
        _log(logger, f"[读取] 失败: {file_path.name}: {track.load_error}")
    if track.load_error is None:
        duration = f"{track.duration:.3f}s" if track.duration is not None else "unknown"
        _log(logger, f"[读取] 标签: {_tag_summary(track)}")
        _log(logger, f"[读取] 歌词: {_lyrics_summary(track.lyrics)}")
        _log(
            logger,
            f"[读取] 封面: {track.cover_mime or 'none'}, {len(track.cover or b'')} bytes",
        )
        _log(logger, f"[读取] 完成: {file_path.name}, duration={duration}")
    return track


def _set_id3_text(tag: ID3, frame_id: str, frame_type: type, value: str) -> None:
    tag.delall(frame_id)
    text = (value or "").strip()
    if text:
        tag.add(frame_type(encoding=3, text=[text]))


def _write_id3(tag: ID3, track: Track) -> None:
    _set_id3_text(tag, "TIT2", TIT2, track.tags.get("title", ""))
    _set_id3_text(tag, "TPE1", TPE1, track.tags.get("artist", ""))
    _set_id3_text(tag, "TALB", TALB, track.tags.get("album", ""))
    _set_id3_text(tag, "TDRC", TDRC, track.tags.get("year", ""))
    _set_id3_text(tag, "TRCK", TRCK, track.tags.get("track", ""))
    _set_id3_text(tag, "TPOS", TPOS, track.tags.get("disc", ""))
    _set_id3_text(tag, "TCON", TCON, track.tags.get("genre", ""))
    _set_id3_text(tag, "TPE2", TPE2, track.tags.get("album_artist", ""))
    _set_id3_text(tag, "TCOM", TCOM, track.tags.get("composer", ""))
    _set_id3_text(tag, "TEXT", TEXT, track.tags.get("lyricist", ""))

    tag.delall("COMM")
    comment = track.tags.get("comment", "").strip()
    if comment:
        tag.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))

    tag.delall("USLT")
    if track.lyrics.strip():
        tag.add(USLT(encoding=3, lang="eng", desc="", text=track.lyrics))

    tag.delall("APIC")
    if track.cover:
        tag.add(
            APIC(
                encoding=3,
                mime=track.cover_mime or "image/jpeg",
                type=3,
                desc="Cover",
                data=track.cover,
            )
        )


def _save_id3(path: Path, track: Track) -> None:
    try:
        tag = ID3(path)
    except ID3NoHeaderError:
        tag = ID3()
    _write_id3(tag, track)
    tag.save(path, v2_version=4)


def _save_embedded_id3(path: Path, track: Track, container_type: type) -> None:
    audio = container_type(path)
    tag = audio.tags if isinstance(audio.tags, ID3) else ID3()
    _write_id3(tag, track)
    audio.tags = tag
    audio.save()


def _set_vorbis(tags: object, key: str, value: str) -> None:
    text = (value or "").strip()
    if text:
        tags[key] = [text]
    elif key in tags:
        del tags[key]


def _save_vorbis(path: Path, track: Track, container_type: type) -> None:
    audio = container_type(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    _set_vorbis(tags, "title", track.tags.get("title", ""))
    _set_vorbis(tags, "artist", track.tags.get("artist", ""))
    _set_vorbis(tags, "album", track.tags.get("album", ""))
    _set_vorbis(tags, "date", track.tags.get("year", ""))
    _set_vorbis(tags, "tracknumber", track.tags.get("track", ""))
    _set_vorbis(tags, "discnumber", track.tags.get("disc", ""))
    _set_vorbis(tags, "genre", track.tags.get("genre", ""))
    _set_vorbis(tags, "albumartist", track.tags.get("album_artist", ""))
    _set_vorbis(tags, "composer", track.tags.get("composer", ""))
    _set_vorbis(tags, "lyricist", track.tags.get("lyricist", ""))
    _set_vorbis(tags, "comment", track.tags.get("comment", ""))
    _set_vorbis(tags, "lyrics", track.lyrics)

    if container_type is FLAC:
        audio.clear_pictures()
        if track.cover:
            picture = Picture()
            picture.type = 3
            picture.mime = track.cover_mime or "image/jpeg"
            picture.desc = "Cover"
            picture.data = track.cover
            audio.add_picture(picture)
    else:
        tags.pop("metadata_block_picture", None)
        if track.cover:
            picture = Picture()
            picture.type = 3
            picture.mime = track.cover_mime or "image/jpeg"
            picture.desc = "Cover"
            picture.data = track.cover
            tags["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def _parse_number_pair(value: str) -> tuple[int, int]:
    match = _NUMBER_RE.search(value or "")
    if not match:
        return 0, 0
    number = int(match.group(1))
    total = int(match.group(2) or 0)
    return number, total


def _set_mp4_text(tags: object, key: str, value: str) -> None:
    text = (value or "").strip()
    if text:
        tags[key] = [text]
    elif key in tags:
        del tags[key]


def _save_mp4(path: Path, track: Track) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    _set_mp4_text(tags, "\xa9nam", track.tags.get("title", ""))
    _set_mp4_text(tags, "\xa9ART", track.tags.get("artist", ""))
    _set_mp4_text(tags, "\xa9alb", track.tags.get("album", ""))
    _set_mp4_text(tags, "\xa9day", track.tags.get("year", ""))
    _set_mp4_text(tags, "\xa9gen", track.tags.get("genre", ""))
    _set_mp4_text(tags, "aART", track.tags.get("album_artist", ""))
    _set_mp4_text(tags, "\xa9wrt", track.tags.get("composer", ""))
    _set_mp4_text(tags, "\xa9cmt", track.tags.get("comment", ""))
    _set_mp4_text(tags, "\xa9lyr", track.lyrics)

    lyricist = track.tags.get("lyricist", "").strip()
    lyricist_key = "----:com.apple.iTunes:LYRICIST"
    if lyricist:
        tags[lyricist_key] = [MP4FreeForm(lyricist.encode("utf-8"))]
    elif lyricist_key in tags:
        del tags[lyricist_key]

    track_number, track_total = _parse_number_pair(track.tags.get("track", ""))
    if track_number:
        tags["trkn"] = [(track_number, track_total)]
    elif "trkn" in tags:
        del tags["trkn"]

    disc_number, disc_total = _parse_number_pair(track.tags.get("disc", ""))
    if disc_number:
        tags["disk"] = [(disc_number, disc_total)]
    elif "disk" in tags:
        del tags["disk"]

    if track.cover:
        image_format = MP4Cover.FORMAT_PNG if track.cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(track.cover, imageformat=image_format)]
    elif "covr" in tags:
        del tags["covr"]
    audio.save()


def _set_asf(tags: object, key: str, value: str) -> None:
    text = (value or "").strip()
    if text:
        tags[key] = [ASFUnicodeAttribute(text)]
    elif key in tags:
        del tags[key]


def _save_asf(path: Path, track: Track) -> None:
    audio = ASF(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    _set_asf(tags, "Title", track.tags.get("title", ""))
    _set_asf(tags, "Author", track.tags.get("artist", ""))
    _set_asf(tags, "WM/AlbumTitle", track.tags.get("album", ""))
    _set_asf(tags, "WM/Year", track.tags.get("year", ""))
    _set_asf(tags, "WM/TrackNumber", track.tags.get("track", ""))
    _set_asf(tags, "WM/PartOfSet", track.tags.get("disc", ""))
    _set_asf(tags, "WM/Genre", track.tags.get("genre", ""))
    _set_asf(tags, "WM/AlbumArtist", track.tags.get("album_artist", ""))
    _set_asf(tags, "WM/Composer", track.tags.get("composer", ""))
    _set_asf(tags, "WM/Lyricist", track.tags.get("lyricist", ""))
    _set_asf(tags, "Description", track.tags.get("comment", ""))
    _set_asf(tags, "WM/Lyrics", track.lyrics)
    audio.save()


def _save_generic_easy(path: Path, track: Track) -> None:
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise TagWriteError("无法写入此音频格式")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    mapping = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "year": "date",
        "track": "tracknumber",
        "disc": "discnumber",
        "genre": "genre",
        "album_artist": "albumartist",
        "composer": "composer",
        "lyricist": "lyricist",
        "comment": "comment",
    }
    for field_name, easy_key in mapping.items():
        _set_vorbis(tags, easy_key, track.tags.get(field_name, ""))
    _set_vorbis(tags, "lyrics", track.lyrics)
    audio.save()


def save_track_tags(track: Track, logger: LogFn | None = None) -> None:
    path = track.path
    suffix = path.suffix.lower()
    track.tags = {key: str(track.tags.get(key, "") or "") for key, _ in FIELD_LABELS}
    _log(logger, f"[保存] 文件: {path} ({suffix or 'unknown'})")
    _log(logger, f"[保存] 写入标签: {_tag_summary(track)}")
    _log(logger, f"[保存] 写入歌词: {_lyrics_summary(track.lyrics)}")
    _log(
        logger,
        f"[保存] 写入封面: {track.cover_mime or 'none'}, {len(track.cover or b'')} bytes",
    )
    try:
        if suffix == ".mp3":
            _save_id3(path, track)
        elif suffix == ".flac":
            _save_vorbis(path, track, FLAC)
        elif suffix in (".m4a", ".mp4"):
            _save_mp4(path, track)
        elif suffix in (".ogg", ".oga"):
            _save_vorbis(path, track, OggVorbis)
        elif suffix == ".opus":
            _save_vorbis(path, track, OggOpus)
        elif suffix in (".wma", ".asf"):
            _save_asf(path, track)
        elif suffix == ".wav":
            _save_embedded_id3(path, track, WAVE)
        elif suffix in (".aif", ".aiff"):
            _save_embedded_id3(path, track, AIFF)
        else:
            _save_generic_easy(path, track)
    except Exception as exc:
        _log(logger, f"[保存] 失败: {path.name}: {exc}")
        raise TagWriteError(str(exc)) from exc
    track.dirty = False
    _log(logger, f"[保存] 完成: {path.name}")


def read_text_file(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def text_similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if len(left_norm) >= 2 and len(right_norm) >= 2 and (
        left_norm in right_norm or right_norm in left_norm
    ):
        return 0.92
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def score_hit(hit: SearchHit, title: str, artist: str, album: str = "") -> float:
    title_score = text_similarity(title, hit.title)
    artist_score = text_similarity(artist, hit.artist)
    album_score = text_similarity(album, hit.album) if album else 0.0
    score = title_score * 0.55 + artist_score * 0.35 + album_score * 0.10
    if not artist.strip():
        score = title_score * 0.76 + album_score * 0.24
    return round(score, 4)


def best_hit(hits: Iterable[SearchHit], title: str, artist: str, album: str = "") -> SearchHit | None:
    best: SearchHit | None = None
    for hit in hits:
        hit.score = score_hit(hit, title, artist, album)
        if best is None or hit.score > best.score:
            best = hit
    return best


def _request_text(
    url: str,
    params: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
    logger: LogFn | None = None,
) -> str:
    if params:
        separator = "&" if "?" in url else "?"
        url = url + separator + urllib.parse.urlencode(params)
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "identity",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    _log_request(logger, request)
    raw = _read_url_response(request, timeout, logger)
    return raw.decode("utf-8-sig", errors="replace")


def download_bytes(url: str, logger: LogFn | None = None, timeout: float = 15.0) -> bytes | None:
    if not url:
        return None
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
        },
    )
    _log_request(logger, request)
    raw = _read_url_response(request, timeout, logger)
    return raw or None


def _request_json(
    url: str,
    params: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
    logger: LogFn | None = None,
) -> dict:
    text = _request_text(url, params, headers, timeout, logger).strip()
    if text.startswith("callback(") and text.endswith(")"):
        text = text[9:-1]
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError("接口返回的不是有效 JSON") from exc
    return result if isinstance(result, dict) else {}


def _request_json_post(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
    logger: LogFn | None = None,
) -> dict:
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json;charset=UTF-8",
    }
    if headers:
        request_headers.update(headers)
    body_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body = body_text.encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    _log_request(logger, request, body_text)
    raw = _read_url_response(request, timeout, logger)
    try:
        result = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProviderError("接口返回的不是有效 JSON") from exc
    return result if isinstance(result, dict) else {}


def parse_netease_search(payload: dict) -> list[SearchHit]:
    songs = payload.get("result", {}).get("songs", []) or []
    hits: list[SearchHit] = []
    for song in songs:
        artists = "/".join(
            str(artist.get("name", "")) for artist in song.get("artists", []) if artist.get("name")
        )
        album = song.get("album") or {}
        duration_ms = song.get("duration") or song.get("dt")
        publish_time = album.get("publishTime") or song.get("publishTime")
        year = ""
        if publish_time:
            try:
                year = time.strftime("%Y", time.localtime(float(publish_time) / 1000.0))
            except (OSError, OverflowError, ValueError):
                year = ""
        track_number = song.get("no") or song.get("trackNumber")
        disc_number = song.get("cd") or song.get("discNumber")
        aliases = song.get("alia") or song.get("alias") or []
        comment = "/".join(str(alias) for alias in aliases if alias)
        hits.append(
            SearchHit(
                source="网易云",
                track_id=str(song.get("id", "")),
                title=str(song.get("name", "")),
                artist=artists,
                album=str(album.get("name", "")),
                duration=float(duration_ms) / 1000.0 if duration_ms else None,
                year=year,
                track=str(track_number or ""),
                disc=str(disc_number or ""),
                comment=comment,
                cover_url=str(album.get("picUrl") or ""),
                album_id=str(album.get("id") or ""),
            )
        )
    return [hit for hit in hits if hit.track_id and hit.title]


def parse_qq_search(payload: dict) -> list[SearchHit]:
    songs = payload.get("req_0", {}).get("data", {}).get("body", {}).get("song", {}).get("list", []) or []
    if not songs:
        songs = payload.get("data", {}).get("song", {}).get("list", []) or []
    hits: list[SearchHit] = []
    for song in songs:
        artists = "/".join(str(singer.get("name", "")) for singer in song.get("singer", []) if singer.get("name"))
        song_mid = song.get("songmid") or song.get("mid")
        duration = song.get("interval")
        album = song.get("album") or {}
        album_mid = str(album.get("mid") or "")
        release_date = str(song.get("time_public") or "")
        year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""
        track_number = song.get("index_album") or song.get("track")
        disc_number = song.get("index_cd") or song.get("disc")
        genre = str(song.get("genre") or "")
        comment = html.unescape(str(song.get("subtitle") or ""))
        cover_url = (
            f"https://y.qq.com/music/photo_new/T002R800x800M000{album_mid}.jpg"
            if album_mid
            else ""
        )
        hits.append(
            SearchHit(
                source="QQ音乐",
                track_id=str(song_mid or ""),
                title=html.unescape(str(song.get("title") or song.get("songname") or song.get("name") or "")),
                artist=artists,
                album=html.unescape(str(album.get("name") or song.get("albumname") or "")),
                duration=float(duration) if duration else None,
                numeric_id=str(song.get("id") or ""),
                year=year,
                genre=genre,
                track=str(track_number or ""),
                disc=str(disc_number or ""),
                comment=comment,
                cover_url=cover_url,
            )
        )
    return [hit for hit in hits if hit.track_id and hit.title]


def merge_translation(original: str, translation: str) -> str:
    if not original.strip() or not translation.strip():
        return original.strip()
    translated_lines: dict[tuple[int, int], str] = {}
    for line in translation.splitlines():
        match = _TIMESTAMP_RE.match(line.strip())
        if not match:
            continue
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        text = line[match.end() :].strip()
        if text:
            translated_lines[(minutes, seconds)] = text
    if not translated_lines:
        return original.strip()

    output: list[str] = []
    for line in original.splitlines():
        output.append(line.rstrip())
        match = _TIMESTAMP_RE.match(line.strip())
        if not match:
            continue
        key = (int(match.group(1)), int(match.group(2)))
        translated = translated_lines.get(key, "")
        original_text = line[match.end() :].strip()
        if translated and translated != original_text:
            output.append(match.group(0) + translated)
    return "\n".join(output).strip()


class NetEaseProvider:
    name = "网易云"

    def search(self, query: str, limit: int = 12, logger: LogFn | None = None) -> list[SearchHit]:
        payload = _request_json(
            "https://music.163.com/api/search/get/web",
            {
                "csrf_token": "",
                "s": query,
                "type": 1,
                "offset": 0,
                "total": "true",
                "limit": limit,
            },
            {"Referer": "https://music.163.com/"},
            logger=logger,
        )
        return parse_netease_search(payload)

    def lyric(self, hit: SearchHit, logger: LogFn | None = None) -> str:
        payload = _request_json(
            "https://music.163.com/api/song/lyric",
            {"id": hit.track_id, "lv": 1, "kv": 1, "tv": 1},
            {"Referer": "https://music.163.com/"},
            logger=logger,
        )
        original = str((payload.get("lrc") or {}).get("lyric", "")).strip()
        if not original:
            raise ProviderError("该歌曲没有可用歌词")
        translated = str((payload.get("tlyric") or {}).get("lyric", "")).strip()
        return merge_translation(original, translated)


class QQProvider:
    name = "QQ音乐"

    def search(self, query: str, limit: int = 12, logger: LogFn | None = None) -> list[SearchHit]:
        request = {
            "req_0": {
                "method": "DoSearchForQQMusicDesktop",
                "module": "music.search.SearchCgiService",
                "param": {
                    "search_type": 0,
                    "query": query,
                    "page_num": 1,
                    "num_per_page": limit,
                },
            }
        }
        payload = _request_json_post(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            request,
            {"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"},
            logger=logger,
        )
        hits = parse_qq_search(payload)
        if hits:
            return hits

        payload = _request_json(
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            {
                "ct": 24,
                "qqmusic_ver": 1298,
                "new_json": 1,
                "remoteplace": "txt.yqq.song",
                "searchid": 1,
                "t": 0,
                "aggr": 1,
                "cr": 1,
                "catZhida": 1,
                "lossless": 0,
                "flag_qc": 0,
                "p": 1,
                "n": limit,
                "w": query,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
            },
            {"Referer": "https://y.qq.com/"},
            logger=logger,
        )
        return parse_qq_search(payload)

    def lyric(self, hit: SearchHit, logger: LogFn | None = None) -> str:
        payload = _request_json(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            {
                "songmid": hit.track_id,
                "format": "json",
                "nobase64": 1,
                "g_tk": 5381,
                "songtype": 0,
                "utf8": 1,
            },
            {"Referer": "https://y.qq.com/"},
            logger=logger,
        )
        lyric = html.unescape(str(payload.get("lyric", ""))).strip()
        if not lyric:
            raise ProviderError("该歌曲没有可用歌词")
        return lyric


PROVIDERS = {
    "网易云": NetEaseProvider(),
    "QQ音乐": QQProvider(),
}


def search_sources(
    query: str,
    source_names: Iterable[str] | None = None,
    limit_per_source: int = 8,
    logger: LogFn | None = None,
) -> tuple[list[SearchHit], list[str]]:
    names = list(source_names or PROVIDERS.keys())
    hits: list[SearchHit] = []
    errors: list[str] = []
    providers = [(name, PROVIDERS[name]) for name in names if name in PROVIDERS]
    _log(logger, f"[搜索] query={query!r}, sources={', '.join(name for name, _ in providers)}")

    def search_one(item: tuple[str, object]) -> tuple[str, list[SearchHit], str | None]:
        name, provider = item
        try:
            result = provider.search(query, limit_per_source, logger)
            _log(logger, f"[搜索] {name} 返回 {len(result)} 条候选")
            return name, result, None
        except Exception as exc:
            _log(logger, f"[搜索] {name} 失败: {exc}")
            return name, [], str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(providers)))) as executor:
        futures = [executor.submit(search_one, provider) for provider in providers]
        for future in as_completed(futures):
            name, result, error_message = future.result()
            hits.extend(result)
            if error_message:
                errors.append(f"{name}: {error_message}")
    return hits, errors


def fetch_lyric(hit: SearchHit, logger: LogFn | None = None) -> str:
    provider = PROVIDERS.get(hit.source)
    if provider is None:
        raise ProviderError("未知歌词来源")
    _log(logger, f"[歌词] 请求: {hit.source} {hit.title} - {hit.artist}")
    lyric = provider.lyric(hit, logger)
    _log(logger, f"[歌词] 收到: {len(lyric)} 字符 | {_shorten(lyric)}")
    return lyric


def fetch_netease_album_year(
    album_id: str,
    logger: LogFn | None = None,
) -> str:
    if not album_id.isdigit():
        return ""
    payload = _request_json(
        f"https://music.163.com/api/v1/album/{album_id}",
        {},
        {"Referer": "https://music.163.com/"},
        logger=logger,
    )
    album = payload.get("album") or {}
    publish_time = album.get("publishTime")
    if not publish_time:
        return ""
    try:
        return time.strftime("%Y", time.localtime(float(publish_time) / 1000.0))
    except (OSError, OverflowError, ValueError):
        return ""


def resolve_netease_album_cover(
    hit: SearchHit,
    logger: LogFn | None = None,
) -> str:
    if hit.source != "网易云" or not hit.album_id.isdigit():
        return hit.cover_url
    payload = _request_json(
        f"https://music.163.com/api/v1/album/{hit.album_id}",
        {},
        {"Referer": "https://music.163.com/"},
        logger=logger,
    )
    album = payload.get("album") or {}
    return str(album.get("picUrl") or hit.cover_url)


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"
