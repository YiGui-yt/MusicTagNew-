from __future__ import annotations

import queue
import json
from datetime import datetime
from pathlib import Path

from PIL import Image
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    QSize,
    Qt,
    QTimer,
    QThreadPool,
    QRunnable,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialogButtonBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core import (
    FIELD_LABELS,
    SUPPORTED_EXTENSIONS,
    SearchHit,
    TagWriteError,
    Track,
    best_hit,
    download_bytes,
    fetch_netease_album_year,
    fetch_lyric,
    format_duration,
    load_track,
    read_text_file,
    resolve_netease_album_cover,
    save_track_tags,
    score_hit,
    search_sources,
)


APP_TITLE = "音乐标签 Python"
SOURCE_MODES = ("网易云 + QQ音乐", "网易云", "QQ音乐")


def selected_source_names(value: str) -> list[str]:
    if value == "网易云":
        return ["网易云"]
    if value == "QQ音乐":
        return ["QQ音乐"]
    return ["网易云", "QQ音乐"]


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(object)
    progress = Signal(int, str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, work) -> None:
        super().__init__()
        self.work = work
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.work(lambda value, message: self.signals.progress.emit(value, message))
        except Exception as exc:
            try:
                self.signals.error.emit(exc)
            except RuntimeError:
                pass
            return
        try:
            self.signals.result.emit(result)
        except RuntimeError:
            pass
        try:
            self.signals.finished.emit()
        except RuntimeError:
            pass


class TrackTableModel(QAbstractTableModel):
    COLUMNS = (
        ("file", "文件名"),
        ("title", "标题"),
        ("artist", "艺术家"),
        ("album", "专辑"),
        ("year", "年份"),
        ("track", "音轨号"),
        ("duration", "时长"),
        ("status", "状态"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.tracks: list[Track] = []

    def set_tracks(self, tracks: list[Track]) -> None:
        self.beginResetModel()
        self.tracks = tracks
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                return self.COLUMNS[section][1]
            return str(section + 1)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.tracks)):
            return None
        track = self.tracks[index.row()]
        key = self.COLUMNS[index.column()][0]
        if role == Qt.ItemDataRole.DisplayRole:
            if key == "file":
                return track.path.name
            if key == "duration":
                return format_duration(track.duration)
            if key == "status":
                if track.load_error:
                    return "读取失败"
                if track.dirty:
                    return "未保存"
                return "有歌词" if track.lyrics.strip() else "无歌词"
            return track.tags.get(key, "")
        if role == Qt.ItemDataRole.ForegroundRole:
            if track.load_error:
                return QColor("#b42318")
            if track.dirty:
                return QColor("#9a6700")
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(track.path)
        return None

    def track_at(self, row: int) -> Track:
        return self.tracks[row]

    def refresh(self) -> None:
        if self.tracks:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.tracks) - 1, len(self.COLUMNS) - 1),
            )


class TrackFilterProxyModel(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.needle = ""

    def set_filter(self, text: str) -> None:
        self.needle = text.strip().casefold()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self.needle:
            return True
        model = self.sourceModel()
        if not isinstance(model, TrackTableModel):
            return True
        track = model.tracks[source_row]
        haystack = " ".join(
            (
                track.path.name,
                track.tags.get("title", ""),
                track.tags.get("artist", ""),
                track.tags.get("album", ""),
                track.tags.get("genre", ""),
            )
        ).casefold()
        return self.needle in haystack


class LyricSearchDialog(QDialog):
    def __init__(
        self,
        app: "MusicTagApp",
        track: Track,
        query: str,
        source_names: list[str],
    ) -> None:
        super().__init__(app)
        self.app_window = app
        self.track = track
        self.source_names = source_names
        self.hits: list[SearchHit] = []
        self.search_token = 0
        self.has_searched = False
        self.setWindowTitle("搜索歌词")
        self.resize(980, 680)
        self._build(query)

    def _build(self, query: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索"))
        self.query_edit = QLineEdit(query)
        self.query_edit.returnPressed.connect(self.search)
        search_row.addWidget(self.query_edit, 1)
        self.search_button = QPushButton(self.app_window.icon("search"), "搜索")
        self.search_button.clicked.connect(self.search)
        search_row.addWidget(self.search_button)
        search_row.addWidget(QLabel("来源: " + "/".join(self.source_names)))
        self.refresh_button = QPushButton(self.app_window.icon("refresh"), "刷新列表")
        self.refresh_button.setToolTip("重新发送当前关键词和来源的请求")
        self.refresh_button.setEnabled(False)
        self.refresh_button.clicked.connect(self.refresh_results)
        search_row.addWidget(self.refresh_button)
        root.addLayout(search_row)

        split = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(split, 1)

        self.results_table = QTableWidget(0, 6)
        self.results_table.setHorizontalHeaderLabels(("来源", "歌曲", "艺术家", "专辑", "时长", "匹配度"))
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.results_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.results_table.doubleClicked.connect(lambda _index: self.load_selected(save=False))
        split.addWidget(self.results_table)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(QLabel("歌词预览"))
        self.preview_edit = QTextEdit()
        self.preview_edit.setReadOnly(True)
        preview_layout.addWidget(self.preview_edit)
        split.addWidget(preview_panel)
        split.setSizes([320, 260])

        footer = QHBoxLayout()
        self.status_label = QLabel("输入关键词后搜索")
        self.status_label.setObjectName("muted")
        footer.addWidget(self.status_label, 1)
        self.preview_button = QPushButton("加载预览")
        self.preview_button.clicked.connect(lambda: self.load_selected(save=False))
        footer.addWidget(self.preview_button)
        self.apply_button = QPushButton(self.app_window.icon("save"), "应用并保存")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(lambda: self.load_selected(save=True))
        footer.addWidget(self.apply_button)
        root.addLayout(footer)

    def search(self, refresh: bool = False) -> None:
        query = self.query_edit.text().strip()
        if not query:
            self.status_label.setText("请输入搜索关键词")
            return
        self.search_token += 1
        token = self.search_token
        self.search_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status_label.setText("正在刷新列表..." if refresh and self.has_searched else "正在搜索...")

        def work(_progress):
            return search_sources(query, self.source_names, logger=self.app_window.log_queue.put)

        def done(result) -> None:
            if token != self.search_token:
                return
            hits, errors = result
            for hit in hits:
                hit.score = score_hit(
                    hit,
                    self.track.tags.get("title", "") or self.track.path.stem,
                    self.track.tags.get("artist", ""),
                    self.track.tags.get("album", ""),
                )
            self.hits = sorted(hits, key=lambda item: item.score, reverse=True)
            self.results_table.setRowCount(len(self.hits))
            for row, hit in enumerate(self.hits):
                values = (
                    hit.source,
                    hit.title,
                    hit.artist,
                    hit.album,
                    format_duration(hit.duration),
                    f"{hit.score:.0%}",
                )
                for column, value in enumerate(values):
                    self.results_table.setItem(row, column, QTableWidgetItem(value))
                self.results_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, hit)
            self.search_button.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.has_searched = True
            if self.hits:
                message = f"找到 {len(self.hits)} 个候选"
                if errors:
                    message += "；部分来源失败: " + "; ".join(errors)
                self.results_table.selectRow(0)
            else:
                message = "没有找到结果"
                if errors:
                    message += ": " + "; ".join(errors)
            self.status_label.setText(message)

        def error(exc: Exception) -> None:
            if token != self.search_token:
                return
            self.search_button.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.has_searched = True
            self.status_label.setText("搜索失败")
            QMessageBox.critical(self, "搜索失败", str(exc))

        self.app_window.start_worker(work, done, error)

    def refresh_results(self) -> None:
        self.search(refresh=True)

    def selected_hit(self) -> SearchHit | None:
        row = self.results_table.currentRow()
        if 0 <= row < len(self.hits):
            return self.hits[row]
        return None

    def load_selected(self, save: bool) -> None:
        hit = self.selected_hit()
        if hit is None:
            QMessageBox.information(self, "未选择结果", "请先选择一个歌词候选。")
            return
        self.preview_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status_label.setText(f"正在加载 {hit.source} 歌词...")

        def work(_progress):
            return fetch_lyric(hit, logger=self.app_window.log_queue.put)

        def done(lyric: str) -> None:
            self.preview_edit.setPlainText(lyric)
            self.app_window.apply_lyric_to_editor(lyric)
            self.app_window.log(f"歌词预览: {hit.source} {hit.caption}")
            self.preview_button.setEnabled(True)
            self.apply_button.setEnabled(True)
            self.status_label.setText(f"已加载: {hit.source} {hit.caption}")
            if save:
                self.track.lyrics = lyric
                self.track.dirty = True
                save_track_tags(self.track, logger=self.app_window.log_queue.put)
                self.app_window.refresh_tracks()
                self.app_window.log(f"歌词已写入: {self.track.path.name}")
                self.status_label.setText("歌词已写入并保存")

        def error(exc: Exception) -> None:
            self.preview_button.setEnabled(True)
            self.apply_button.setEnabled(True)
            self.status_label.setText("歌词加载失败")
            QMessageBox.critical(self, "歌词加载失败", str(exc))

        self.app_window.start_worker(work, done, error)


COMBINED_OVERWRITE_DEFAULTS: tuple[tuple[str, str, bool], ...] = (
    ("title", "标题", True),
    ("artist", "艺术家", True),
    ("album", "专辑", True),
    ("year", "年份", True),
    ("track", "音轨号", True),
    ("disc", "碟号", True),
    ("genre", "风格", True),
    ("comment", "注释", False),
    ("lyrics", "歌词", True),
    ("cover", "封面", True),
)


class OverwriteOptionsDialog(QDialog):
    SETTINGS_PATH = Path(__file__).with_name("combined_tag_settings.json")

    def __init__(self, current: dict[str, bool], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("覆盖选项")
        self.resize(360, 430)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.addWidget(QLabel("选择应用组合标签时覆盖的字段："))
        grid = QGridLayout()
        self.checks: dict[str, QCheckBox] = {}
        for index, (key, label, default) in enumerate(COMBINED_OVERWRITE_DEFAULTS):
            check = QCheckBox(label)
            check.setChecked(bool(current.get(key, default)))
            self.checks[key] = check
            grid.addWidget(check, index // 2, index % 2)
        root.addLayout(grid)
        root.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self) -> dict[str, bool]:
        return {key: check.isChecked() for key, check in self.checks.items()}

    @classmethod
    def load_saved(cls) -> dict[str, bool]:
        values = {key: default for key, _label, default in COMBINED_OVERWRITE_DEFAULTS}
        try:
            raw = json.loads(cls.SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in values:
                    if isinstance(raw.get(key), bool):
                        values[key] = raw[key]
        except (OSError, json.JSONDecodeError):
            pass
        return values

    @classmethod
    def save_values(cls, values: dict[str, bool]) -> None:
        try:
            cls.SETTINGS_PATH.write_text(
                json.dumps(values, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


class CombinedTagSearchDialog(QDialog):
    def __init__(
        self,
        app: "MusicTagApp",
        track: Track,
        query: str,
        source_names: list[str],
    ) -> None:
        super().__init__(app)
        self.app_window = app
        self.track = track
        self.source_names = source_names
        self.hits: list[SearchHit] = []
        self.search_token = 0
        self.has_searched = False
        self.overwrite_options = OverwriteOptionsDialog.load_saved()
        self.cover_data: bytes | None = None
        self.cover_mime: str | None = None
        self.lyric_text = ""
        self.setWindowTitle("组合标签搜索")
        self.resize(1160, 740)
        self._build(query)

    def _build(self, query: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索"))
        self.query_edit = QLineEdit(query)
        self.query_edit.returnPressed.connect(self.search)
        search_row.addWidget(self.query_edit, 1)
        self.search_button = QPushButton(self.app_window.icon("search"), "搜索标签")
        self.search_button.clicked.connect(self.search)
        search_row.addWidget(self.search_button)
        self.refresh_button = QPushButton(self.app_window.icon("refresh"), "刷新列表")
        self.refresh_button.setEnabled(False)
        self.refresh_button.clicked.connect(self.refresh_results)
        search_row.addWidget(self.refresh_button)
        self.options_button = QPushButton("覆盖选项")
        self.options_button.clicked.connect(self.edit_overwrite_options)
        search_row.addWidget(self.options_button)
        search_row.addWidget(QLabel("来源: " + "/".join(self.source_names)))
        root.addLayout(search_row)

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.results_table = QTableWidget(0, 8)
        self.results_table.setHorizontalHeaderLabels(
            ("来源", "标题", "艺术家", "专辑", "年份", "音轨", "风格", "歌词")
        )
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.results_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.results_table.setAlternatingRowColors(True)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (4, 5, 6, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.results_table.itemSelectionChanged.connect(self._show_selected_details)
        self.results_table.doubleClicked.connect(lambda _index: self.accept_selected())
        table_layout.addWidget(self.results_table, 1)
        split.addWidget(table_panel)

        details = QGroupBox("选中候选")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(10, 12, 10, 10)
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(5)
        self.detail_labels: dict[str, QLabel] = {}
        for row, (key, label) in enumerate(
            (
                ("source", "来源"),
                ("title", "标题"),
                ("artist", "艺术家"),
                ("album", "专辑"),
                ("year", "年份"),
                ("track", "音轨号"),
                ("disc", "碟号"),
                ("genre", "风格"),
                ("comment", "注释"),
            )
        ):
            form.addWidget(QLabel(label), row, 0)
            value = QLabel("")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.detail_labels[key] = value
            form.addWidget(value, row, 1)
        details_layout.addLayout(form)
        self.cover_preview = QLabel("无封面")
        self.cover_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview.setMinimumSize(180, 150)
        self.cover_preview.setStyleSheet(
            "background: #eef3f5; border-radius: 4px; color: #65727e;"
        )
        details_layout.addWidget(self.cover_preview)
        self.lyric_status = QLabel("歌词: 尚未加载")
        self.lyric_status.setWordWrap(True)
        details_layout.addWidget(self.lyric_status)
        self.lyric_preview = QTextEdit()
        self.lyric_preview.setReadOnly(True)
        self.lyric_preview.setMinimumHeight(150)
        details_layout.addWidget(self.lyric_preview, 1)
        split.addWidget(details)
        split.setSizes([760, 360])

        footer = QHBoxLayout()
        self.status_label = QLabel("正在搜索候选标签...")
        footer.addWidget(self.status_label, 1)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        footer.addWidget(cancel_button)
        self.apply_button = QPushButton(self.app_window.icon("save"), "应用到编辑器")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.accept_selected)
        footer.addWidget(self.apply_button)
        root.addLayout(footer)

    def search(self, refresh: bool = False) -> None:
        query = self.query_edit.text().strip()
        if not query:
            self.status_label.setText("请输入搜索关键词")
            return
        self.search_token += 1
        token = self.search_token
        self.search_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status_label.setText("正在刷新候选标签..." if refresh and self.has_searched else "正在搜索候选标签...")

        def work(_progress):
            return search_sources(
                query,
                self.source_names,
                logger=self.app_window.log_queue.put,
            )

        def done(result) -> None:
            if token != self.search_token:
                return
            hits, errors = result
            for hit in hits:
                hit.score = score_hit(
                    hit,
                    self.track.tags.get("title", "") or self.track.path.stem,
                    self.track.tags.get("artist", ""),
                    self.track.tags.get("album", ""),
                )
            self.hits = sorted(hits, key=lambda item: item.score, reverse=True)
            self.results_table.setRowCount(len(self.hits))
            for row, hit in enumerate(self.hits):
                lyric_state = "可获取" if hit.track_id else ""
                values = (
                    hit.source,
                    hit.title,
                    hit.artist,
                    hit.album,
                    hit.year,
                    hit.track,
                    hit.genre,
                    lyric_state,
                )
                for column, value in enumerate(values):
                    self.results_table.setItem(row, column, QTableWidgetItem(value))
            self.search_button.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.has_searched = True
            if self.hits:
                self.results_table.selectRow(0)
                message = f"找到 {len(self.hits)} 个候选"
                if errors:
                    message += "；部分来源失败: " + "; ".join(errors)
            else:
                message = "没有找到候选标签"
                if errors:
                    message += ": " + "; ".join(errors)
            self.status_label.setText(message)

        def error(exc: Exception) -> None:
            if token != self.search_token:
                return
            self.search_button.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.has_searched = True
            self.status_label.setText("搜索失败")
            QMessageBox.critical(self, "组合标签搜索失败", str(exc))

        self.app_window.start_worker(work, done, error)

    def refresh_results(self) -> None:
        self.search(refresh=True)

    def edit_overwrite_options(self) -> None:
        dialog = OverwriteOptionsDialog(self.overwrite_options, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.overwrite_options = dialog.values()
            OverwriteOptionsDialog.save_values(self.overwrite_options)
            self.status_label.setText("覆盖选项已保存")
            self._refresh_field_availability()

    def _refresh_field_availability(self) -> None:
        hit = self.selected_hit()
        if hit is None:
            return
        selected_values = {
            "title": hit.title,
            "artist": hit.artist,
            "album": hit.album,
            "year": hit.year,
            "track": hit.track,
            "disc": hit.disc,
            "genre": hit.genre,
            "comment": hit.comment,
        }
        missing = [
            label
            for key, label in (
                ("title", "标题"),
                ("artist", "艺术家"),
                ("album", "专辑"),
                ("year", "年份"),
                ("track", "音轨号"),
                ("disc", "碟号"),
                ("genre", "风格"),
                ("comment", "注释"),
            )
            if self.overwrite_options.get(key) and not selected_values[key].strip()
        ]
        if missing:
            self.status_label.setText("该候选没有可覆盖的字段: " + "、".join(missing))

    def selected_hit(self) -> SearchHit | None:
        row = self.results_table.currentRow()
        if 0 <= row < len(self.hits):
            return self.hits[row]
        return None

    def _show_selected_details(self) -> None:
        hit = self.selected_hit()
        self.cover_data = None
        self.cover_mime = None
        self.lyric_text = ""
        self.cover_preview.setPixmap(QPixmap())
        self.cover_preview.setText("无封面")
        self.lyric_preview.clear()
        if hit is None:
            for label in self.detail_labels.values():
                label.setText("")
            self.lyric_status.setText("歌词: 尚未加载")
            return
        values = {
            "source": hit.source,
            "title": hit.title,
            "artist": hit.artist,
            "album": hit.album,
            "year": hit.year,
            "track": hit.track,
            "disc": hit.disc,
            "genre": hit.genre,
            "comment": hit.comment,
        }
        for key, label in self.detail_labels.items():
            label.setText(values.get(key, ""))
        self.lyric_status.setText("歌词: 正在加载...")
        self._refresh_field_availability()
        self._load_selected_media(hit)

    def _load_selected_media(self, hit: SearchHit) -> None:
        token = self.search_token

        def work(_progress):
            lyric = ""
            try:
                lyric = fetch_lyric(hit, logger=self.app_window.log_queue.put)
            except Exception as exc:
                self.app_window.log_queue.put(f"[组合标签] 歌词获取失败: {exc}")
            cover = None
            try:
                cover_url = hit.cover_url
                if not cover_url and hit.source == "网易云" and hit.album_id:
                    cover_url = resolve_netease_album_cover(
                        hit,
                        logger=self.app_window.log_queue.put,
                    )
                cover = download_bytes(cover_url, logger=self.app_window.log_queue.put)
            except Exception as exc:
                self.app_window.log_queue.put(f"[组合标签] 封面获取失败: {exc}")
            year = hit.year
            if not year and hit.source == "网易云" and hit.album_id:
                try:
                    year = fetch_netease_album_year(
                        hit.album_id,
                        logger=self.app_window.log_queue.put,
                    )
                except Exception as exc:
                    self.app_window.log_queue.put(f"[组合标签] 年份获取失败: {exc}")
            return lyric, cover, year

        def done(result) -> None:
            current = self.selected_hit()
            if token != self.search_token or current is not hit:
                return
            lyric, cover, year = result
            if year and not hit.year:
                hit.year = year
                self.detail_labels["year"].setText(year)
                row = self.results_table.currentRow()
                if row >= 0:
                    year_item = self.results_table.item(row, 4)
                    if year_item is not None:
                        year_item.setText(year)
                self._refresh_field_availability()
            self.lyric_text = lyric
            self.lyric_preview.setPlainText(lyric)
            self.lyric_status.setText(
                f"歌词: {len(lyric)} 字符" if lyric else "歌词: 没有可用歌词"
            )
            if cover:
                pixmap = QPixmap()
                if pixmap.loadFromData(cover):
                    self.cover_data = cover
                    self.cover_mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
                    self.cover_preview.setText("")
                    self.cover_preview.setPixmap(
                        pixmap.scaled(
                            240,
                            180,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                else:
                    self.cover_preview.setText("封面无法预览")
            else:
                self.cover_preview.setText("无封面")

        def error(exc: Exception) -> None:
            self.lyric_status.setText(f"候选详情加载失败: {exc}")

        self.app_window.start_worker(work, done, error)

    def accept_selected(self) -> None:
        hit = self.selected_hit()
        if hit is None:
            QMessageBox.information(self, "未选择候选", "请先选择一个组合标签候选。")
            return
        values = {
            "title": hit.title,
            "artist": hit.artist,
            "album": hit.album,
            "year": hit.year,
            "track": hit.track,
            "disc": hit.disc,
            "genre": hit.genre,
            "comment": hit.comment,
            "lyrics": self.lyric_text,
        }
        self.app_window.apply_combined_tag(
            values,
            self.overwrite_options,
            self.cover_data,
            self.cover_mime,
            missing_year=(
                self.overwrite_options.get("year")
                and not hit.year.strip()
                and hit.source == "网易云"
                and bool(hit.numeric_id)
            ),
        )
        self.accept()


class MusicTagApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1440, 860)
        self.setMinimumSize(1080, 680)

        self.tracks: list[Track] = []
        self.track_by_path: dict[str, Track] = {}
        self.current_tracks: list[Track] = []
        self.field_edits: dict[str, QLineEdit] = {}
        self.editor_snapshot: dict[str, str] = {}
        self.lyrics_snapshot = ""
        self.editor_dirty = False
        self.loading_editor = False
        self.cover_bytes: bytes | None = None
        self.cover_mime: str | None = None
        self.cover_dirty = False
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.thread_pool = QThreadPool.globalInstance()
        self.workers: set[Worker] = set()

        self.track_model = TrackTableModel()
        self.proxy_model = TrackFilterProxyModel()
        self.proxy_model.setSourceModel(self.track_model)
        self._build_ui()
        self._build_menu()
        self._set_style()
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(100)
        self.log_timer.timeout.connect(self._drain_log_queue)
        self.log_timer.start()

    def _set_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f3f6f8;
                color: #17212b;
                font-family: "Microsoft YaHei UI";
                font-size: 9pt;
            }
            QMenuBar {
                background: #ffffff;
                border-bottom: 1px solid #d7e0e5;
            }
            QMenuBar::item { padding: 5px 9px; background: transparent; }
            QMenuBar::item:selected { background: #e5eef1; }
            QMenu { background: #ffffff; border: 1px solid #d7e0e5; padding: 4px; }
            QMenu::item { padding: 6px 24px 6px 12px; }
            QMenu::item:selected { background: #d9f0ec; }
            QToolBar {
                background: #ffffff;
                border: 0;
                border-bottom: 1px solid #d7e0e5;
                spacing: 5px;
                padding: 6px 8px;
            }
            QToolBar QToolButton {
                border: 0;
                border-radius: 4px;
                padding: 6px 9px;
                background: #edf2f4;
                color: #17212b;
            }
            QToolBar QToolButton:hover { background: #dfe9ec; }
            QToolBar QToolButton:pressed { background: #d2e0e4; }
            QToolBar QToolButton:disabled { color: #9aa6ad; background: #f2f4f5; }
            QWidget#panel, QGroupBox, QScrollArea, QScrollArea > QWidget > QWidget {
                background: #ffffff;
            }
            QGroupBox {
                border: 1px solid #d7e0e5;
                border-radius: 6px;
                margin-top: 10px;
                padding: 10px 8px 8px 8px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 9px;
                padding: 0 4px;
                color: #17212b;
            }
            QLineEdit, QComboBox, QTextEdit, QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #cfdae0;
                border-radius: 4px;
                padding: 5px 6px;
                selection-background-color: #a7d9d1;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus {
                border: 1px solid #0f766e;
            }
            QPushButton {
                border: 1px solid #cfdae0;
                border-radius: 4px;
                padding: 6px 12px;
                background: #edf2f4;
                color: #17212b;
            }
            QPushButton:hover { background: #dfe9ec; }
            QPushButton:pressed { background: #d1dfe3; }
            QPushButton:disabled { background: #f0f3f4; color: #98a4ab; }
            QPushButton#primary {
                background: #0f766e;
                color: #ffffff;
                border-color: #0f766e;
                font-weight: bold;
            }
            QPushButton#primary:hover { background: #0b655e; }
            QTableView, QTableWidget {
                background: #ffffff;
                alternate-background-color: #f8fafb;
                border: 1px solid #d7e0e5;
                border-radius: 4px;
                gridline-color: #edf1f3;
                selection-background-color: #cce9e4;
                selection-color: #17212b;
            }
            QHeaderView::section {
                background: #e8eef1;
                color: #17212b;
                padding: 6px;
                border: 0;
                border-right: 1px solid #d7e0e5;
                border-bottom: 1px solid #d7e0e5;
                font-weight: bold;
            }
            QStatusBar {
                background: #ffffff;
                border-top: 1px solid #d7e0e5;
            }
            QProgressBar {
                border: 0;
                background: #e5ecef;
                border-radius: 3px;
                text-align: center;
                min-height: 10px;
                max-height: 10px;
                min-width: 180px;
            }
            QProgressBar::chunk { background: #0f766e; border-radius: 3px; }
            QLabel#muted { color: #65727e; }
            QSplitter::handle { background: #d7e0e5; width: 1px; height: 1px; }
            """
        )

    def icon(self, name: str) -> QIcon:
        icons = {
            "open": QStyle.StandardPixmap.SP_DialogOpenButton,
            "folder": QStyle.StandardPixmap.SP_DirOpenIcon,
            "save": QStyle.StandardPixmap.SP_DialogSaveButton,
            "search": QStyle.StandardPixmap.SP_FileDialogContentsView,
            "refresh": QStyle.StandardPixmap.SP_BrowserReload,
            "auto": QStyle.StandardPixmap.SP_MediaPlay,
            "cover": QStyle.StandardPixmap.SP_FileDialogDetailedView,
        }
        return self.style().standardIcon(icons.get(name, QStyle.StandardPixmap.SP_FileIcon))

    def _build_ui(self) -> None:
        toolbar = QToolBar("主要操作")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        self.open_action = QAction(self.icon("open"), "打开文件", self)
        self.open_directory_action = QAction(self.icon("folder"), "打开目录", self)
        self.save_action = QAction(self.icon("save"), "保存标签", self)
        self.search_action = QAction(self.icon("search"), "搜索歌词", self)
        self.auto_action = QAction(self.icon("auto"), "自动匹配歌词", self)
        self.combined_tag_action = QAction(self.icon("cover"), "组合标签", self)
        self.open_action.triggered.connect(self.open_files)
        self.open_directory_action.triggered.connect(self.open_directory)
        self.save_action.triggered.connect(self.save_selected)
        self.search_action.triggered.connect(self.open_lyric_search)
        self.auto_action.triggered.connect(self.auto_match_lyrics)
        self.combined_tag_action.triggered.connect(self.open_combined_tag_search)
        toolbar.addAction(self.open_action)
        toolbar.addAction(self.open_directory_action)
        toolbar.addAction(self.save_action)
        toolbar.addSeparator()
        toolbar.addAction(self.search_action)
        toolbar.addAction(self.auto_action)
        toolbar.addAction(self.combined_tag_action)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.overwrite_check = QCheckBox("覆盖已有歌词")
        toolbar.addWidget(self.overwrite_check)
        toolbar.addWidget(QLabel("来源"))
        self.source_combo = QComboBox()
        self.source_combo.addItems(SOURCE_MODES)
        self.source_combo.setMinimumWidth(150)
        toolbar.addWidget(self.source_combo)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)
        splitter.addWidget(self._build_editor_panel())
        splitter.addWidget(self._build_list_panel())
        splitter.setSizes([430, 1000])
        splitter.setChildrenCollapsible(False)

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel("就绪")
        status.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        status.addPermanentWidget(self.progress_bar)

    def _build_editor_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("panel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(12, 10, 8, 10)
        outer.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("标签编辑")
        title.setStyleSheet("font-size: 12pt; font-weight: bold;")
        header.addWidget(title)
        self.selection_label = QLabel("未选择文件")
        self.selection_label.setObjectName("muted")
        self.selection_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.selection_label, 1)
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)
        content_layout.setSpacing(8)

        tags_group = QGroupBox("标签")
        form = QGridLayout(tags_group)
        form.setContentsMargins(10, 12, 10, 10)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        for index, (key, label) in enumerate(FIELD_LABELS):
            edit = QLineEdit()
            edit.textEdited.connect(self._mark_editor_dirty)
            self.field_edits[key] = edit
            row = index // 2
            column = (index % 2) * 2
            form.addWidget(QLabel(label), row, column)
            form.addWidget(edit, row, column + 1)
        for column in (1, 3):
            form.setColumnStretch(column, 1)
        content_layout.addWidget(tags_group)

        cover_group = QGroupBox("封面")
        cover_layout = QHBoxLayout(cover_group)
        cover_layout.setContentsMargins(10, 12, 10, 10)
        self.cover_label = QLabel("无封面")
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setMinimumSize(220, 150)
        self.cover_label.setMaximumHeight(170)
        self.cover_label.setStyleSheet("background: #eef3f5; border-radius: 4px; color: #65727e;")
        cover_layout.addWidget(self.cover_label, 1)
        cover_buttons = QVBoxLayout()
        choose_cover = QPushButton(self.icon("cover"), "选择封面")
        choose_cover.clicked.connect(self.choose_cover)
        remove_cover = QPushButton("移除封面")
        remove_cover.clicked.connect(self.remove_cover)
        cover_buttons.addWidget(choose_cover)
        cover_buttons.addWidget(remove_cover)
        cover_buttons.addStretch(1)
        cover_layout.addLayout(cover_buttons)
        content_layout.addWidget(cover_group)

        lyrics_group = QGroupBox("歌词")
        lyrics_layout = QVBoxLayout(lyrics_group)
        lyrics_layout.setContentsMargins(10, 12, 10, 10)
        lyrics_tools = QHBoxLayout()
        lyrics_tools.addStretch(1)
        import_button = QPushButton("导入 LRC")
        import_button.clicked.connect(self.import_lrc)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self.clear_editor_lyrics)
        lyrics_tools.addWidget(import_button)
        lyrics_tools.addWidget(clear_button)
        lyrics_layout.addLayout(lyrics_tools)
        self.lyrics_edit = QTextEdit()
        self.lyrics_edit.setMinimumHeight(210)
        self.lyrics_edit.setAcceptRichText(False)
        self.lyrics_edit.textChanged.connect(self._mark_editor_dirty)
        lyrics_layout.addWidget(self.lyrics_edit, 1)
        content_layout.addWidget(lyrics_group, 1)

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return panel

    def _build_list_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 10, 12, 10)
        layout.setSpacing(8)

        table_tools = QHBoxLayout()
        table_tools.addWidget(QLabel("文件列表"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("过滤文件名、标题、艺术家、专辑...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.proxy_model.set_filter)
        table_tools.addWidget(self.filter_edit, 1)
        layout.addLayout(table_tools)

        vertical = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(vertical, 1)

        self.table = QTableView()
        self.table.setModel(self.proxy_model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.doubleClicked.connect(lambda _index: self.open_lyric_search())
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        header = self.table.horizontalHeader()
        widths = (230, 190, 170, 190, 70, 70, 70, 95)
        for index, width in enumerate(widths):
            self.table.setColumnWidth(index, width)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        for index in (0, 1, 2, 3):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        vertical.addWidget(self.table)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(4)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("运行日志"))
        log_header.addStretch(1)
        copy_log = QPushButton("复制日志")
        copy_log.clicked.connect(
            lambda: QApplication.clipboard().setText(self.log_edit.toPlainText())
        )
        log_header.addWidget(copy_log)
        clear_log = QPushButton("清空日志")
        clear_log.clicked.connect(self.clear_log)
        log_header.addWidget(clear_log)
        log_layout.addLayout(log_header)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(500)
        self.log_edit.setMinimumHeight(180)
        log_layout.addWidget(self.log_edit)
        vertical.addWidget(log_panel)
        vertical.setSizes([520, 260])
        vertical.setChildrenCollapsible(False)
        return panel

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("文件")
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.open_directory_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_action)
        file_menu.addAction("导入 LRC...", self.import_lrc)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        lyric_menu = menu_bar.addMenu("歌词")
        lyric_menu.addAction(self.search_action)
        lyric_menu.addAction(self.auto_action)
        lyric_menu.addAction("清空编辑器歌词", self.clear_editor_lyrics)

        tag_menu = menu_bar.addMenu("标签")
        tag_menu.addAction(self.combined_tag_action)

        edit_menu = menu_bar.addMenu("编辑")
        edit_menu.addAction("从列表移除", self.remove_selected_files, QKeySequence("Delete"))
        edit_menu.addAction("重新读取选中文件", self.reload_selected, QKeySequence("F5"))

        help_menu = menu_bar.addMenu("帮助")
        help_menu.addAction("使用说明", self.show_help)
        help_menu.addAction("关于", self.show_about)

        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.search_action.setShortcut(QKeySequence("Ctrl+L"))
        self.auto_action.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.combined_tag_action.setShortcut(QKeySequence("Ctrl+Shift+T"))

    def start_worker(self, work, done, error=None) -> None:
        worker = Worker(work)
        self.workers.add(worker)
        worker.signals.result.connect(done)
        worker.signals.error.connect(error or self.show_error)
        worker.signals.progress.connect(self.update_progress)
        worker.signals.finished.connect(lambda current=worker: self.workers.discard(current))
        self.thread_pool.start(worker)

    def show_error(self, exc: Exception) -> None:
        self.progress_bar.setValue(0)
        self.status_label.setText("操作失败")
        QMessageBox.critical(self, "操作失败", str(exc))

    def update_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(max(0, min(100, value)))
        if message:
            self.status_label.setText(message)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{timestamp}] {message}")
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def enqueue_log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                return
            self.log(message)

    def clear_log(self) -> None:
        self.log_edit.clear()

    def _mark_editor_dirty(self, *_args) -> None:
        if not self.loading_editor:
            self.editor_dirty = True

    def open_files(self) -> None:
        patterns = " ".join(f"*{extension}" for extension in SUPPORTED_EXTENSIONS)
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择音频文件",
            "",
            f"音频文件 ({patterns});;所有文件 (*.*)",
        )
        if paths:
            self.add_files(paths)

    def open_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择音乐目录")
        if not directory:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            paths = [
                str(path)
                for path in Path(directory).rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
            ]
        finally:
            QApplication.restoreOverrideCursor()
        self.add_files(paths)

    def add_files(self, paths: list[str]) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        added = 0
        try:
            for raw_path in paths:
                canonical = str(Path(raw_path).resolve())
                if canonical in self.track_by_path:
                    continue
                track = load_track(canonical, logger=self.log_queue.put)
                self.tracks.append(track)
                self.track_by_path[canonical] = track
                added += 1
                if track.load_error:
                    self.log(f"读取失败: {track.path.name}: {track.load_error}")
        finally:
            QApplication.restoreOverrideCursor()
        self.refresh_tracks()
        if added:
            self.status_label.setText(f"已添加 {added} 个文件")

    def refresh_tracks(self) -> None:
        current_paths = [str(track.path) for track in self.current_tracks]
        self.track_model.set_tracks(self.tracks)
        self.table.clearSelection()
        for row in range(self.track_model.rowCount()):
            source_index = self.track_model.index(row, 0)
            proxy_index = self.proxy_model.mapFromSource(source_index)
            if proxy_index.isValid() and str(self.track_model.track_at(row).path) in current_paths:
                self.table.selectionModel().select(
                    proxy_index,
                    self.table.selectionModel().SelectionFlag.Select
                    | self.table.selectionModel().SelectionFlag.Rows,
                )

    def selected_tracks(self) -> list[Track]:
        seen: set[str] = set()
        result: list[Track] = []
        for proxy_index in self.table.selectionModel().selectedRows():
            source_index = self.proxy_model.mapToSource(proxy_index)
            if not source_index.isValid():
                continue
            track = self.track_model.track_at(source_index.row())
            path = str(track.path)
            if path not in seen:
                seen.add(path)
                result.append(track)
        return result

    def _on_selection_changed(self, *_args) -> None:
        if self.loading_editor:
            return
        if self.editor_dirty and self.current_tracks:
            self._apply_editor_to_tracks(self.current_tracks, changed_only=True, mark_dirty=True)
            self.track_model.refresh()
        self.editor_dirty = False
        self.current_tracks = self.selected_tracks()
        if not self.current_tracks:
            self.selection_label.setText("未选择文件")
            self._clear_editor()
            return
        if len(self.current_tracks) == 1:
            self.selection_label.setText(self.current_tracks[0].path.name)
            self._load_single_track(self.current_tracks[0])
        else:
            self.selection_label.setText(f"已选择 {len(self.current_tracks)} 个文件")
            self._load_common_values(self.current_tracks)
        self.status_label.setText(f"已选择 {len(self.current_tracks)} 个文件")

    def _clear_editor(self) -> None:
        self.loading_editor = True
        try:
            for edit in self.field_edits.values():
                edit.clear()
            self.lyrics_edit.clear()
        finally:
            self.loading_editor = False
        self.editor_snapshot = {}
        self.lyrics_snapshot = ""
        self.cover_dirty = False
        self._set_cover(None, None)

    def _load_single_track(self, track: Track) -> None:
        self.loading_editor = True
        try:
            for key, edit in self.field_edits.items():
                edit.setText(track.tags.get(key, ""))
            self.lyrics_edit.setPlainText(track.lyrics)
        finally:
            self.loading_editor = False
        self.editor_snapshot = track.snapshot()
        self.lyrics_snapshot = track.lyrics
        self.cover_dirty = False
        self._set_cover(track.cover, track.cover_mime)

    def _load_common_values(self, tracks: list[Track]) -> None:
        self.loading_editor = True
        try:
            for key, edit in self.field_edits.items():
                values = {track.tags.get(key, "") for track in tracks}
                value = next(iter(values)) if len(values) == 1 else ""
                edit.setText(value)
                self.editor_snapshot[key] = value
            lyric_values = {track.lyrics for track in tracks}
            common_lyrics = next(iter(lyric_values)) if len(lyric_values) == 1 else ""
            self.lyrics_edit.setPlainText(common_lyrics)
            self.lyrics_snapshot = common_lyrics
        finally:
            self.loading_editor = False
        self.cover_dirty = False
        self._set_cover(None, None)

    def _editor_values(self) -> dict[str, str]:
        return {key: edit.text().strip() for key, edit in self.field_edits.items()}

    def _editor_lyrics(self) -> str:
        return self.lyrics_edit.toPlainText().rstrip("\n")

    def _apply_editor_to_tracks(
        self,
        tracks: list[Track],
        changed_only: bool,
        mark_dirty: bool,
    ) -> None:
        values = self._editor_values()
        lyrics = self._editor_lyrics()
        for track in tracks:
            changed = False
            for key, value in values.items():
                if not changed_only or value != self.editor_snapshot.get(key, ""):
                    if track.tags.get(key, "") != value:
                        track.tags[key] = value
                        changed = True
            if not changed_only or lyrics != self.lyrics_snapshot:
                if track.lyrics != lyrics:
                    track.lyrics = lyrics
                    changed = True
            if self.cover_dirty:
                if track.cover != self.cover_bytes or track.cover_mime != self.cover_mime:
                    track.cover = self.cover_bytes
                    track.cover_mime = self.cover_mime
                    changed = True
            if changed and mark_dirty:
                track.dirty = True
        self.editor_dirty = False

    def save_selected(self) -> None:
        tracks = self.selected_tracks()
        if not tracks:
            QMessageBox.information(self, "未选择文件", "请先在列表中选择要保存的文件。")
            return
        self._apply_editor_to_tracks(tracks, changed_only=True, mark_dirty=True)
        errors: list[str] = []
        for index, track in enumerate(tracks, start=1):
            try:
                save_track_tags(track, logger=self.log_queue.put)
                self.log(f"已保存: {track.path.name}")
            except TagWriteError as exc:
                errors.append(f"{track.path.name}: {exc}")
                self.log(f"保存失败: {track.path.name}: {exc}")
            self.update_progress(index / len(tracks) * 100, f"正在保存 {index}/{len(tracks)}")
        self.cover_dirty = False
        self.track_model.refresh()
        self._on_selection_changed()
        self.update_progress(100, "保存完成" if not errors else "部分文件保存失败")
        if errors:
            QMessageBox.critical(self, "保存失败", "\n".join(errors[:12]))

    def reload_selected(self) -> None:
        tracks = self.selected_tracks()
        if not tracks:
            return
        for track in tracks:
            fresh = load_track(track.path, logger=self.log_queue.put)
            track.tags = fresh.tags
            track.lyrics = fresh.lyrics
            track.cover = fresh.cover
            track.cover_mime = fresh.cover_mime
            track.duration = fresh.duration
            track.load_error = fresh.load_error
            track.dirty = False
        self.refresh_tracks()
        self.log(f"已重新读取 {len(tracks)} 个文件")

    def remove_selected_files(self) -> None:
        tracks = self.selected_tracks()
        if not tracks:
            return
        remove_paths = {str(track.path) for track in tracks}
        self.tracks = [track for track in self.tracks if str(track.path) not in remove_paths]
        for path in remove_paths:
            self.track_by_path.pop(path, None)
        self.current_tracks = []
        self.refresh_tracks()
        self._clear_editor()
        self.log(f"已从列表移除 {len(tracks)} 个文件")

    def import_lrc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入歌词",
            "",
            "歌词文件 (*.lrc *.txt);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            lyrics = read_text_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self.lyrics_edit.setPlainText(lyrics)
        self.log(f"已导入歌词: {Path(path).name}")

    def clear_editor_lyrics(self) -> None:
        self.lyrics_edit.clear()

    def choose_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择封面图片",
            "",
            "图片 (*.jpg *.jpeg *.png *.webp);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            QMessageBox.critical(self, "图片无效", str(exc))
            return
        suffix = Path(path).suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
        self.cover_dirty = True
        self.editor_dirty = True
        self._set_cover(data, mime)

    def remove_cover(self) -> None:
        self.cover_dirty = True
        self.editor_dirty = True
        self._set_cover(None, None)

    def _set_cover(self, data: bytes | None, mime: str | None) -> None:
        self.cover_bytes = data
        self.cover_mime = mime
        if not data:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("无封面")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText("封面无法预览")
            return
        self.cover_label.setText("")
        self.cover_label.setPixmap(
            pixmap.scaled(
                220,
                160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def apply_lyric_to_editor(self, lyric: str) -> None:
        self.lyrics_edit.setPlainText(lyric)
        self.editor_dirty = True

    def open_lyric_search(self) -> None:
        tracks = self.selected_tracks()
        if not tracks:
            QMessageBox.information(self, "未选择文件", "请先选择一个音频文件。")
            return
        track = tracks[0]
        dialog = LyricSearchDialog(
            self,
            track,
            self.track_query(track),
            selected_source_names(self.source_combo.currentText()),
        )
        dialog.exec()

    def open_combined_tag_search(self) -> None:
        tracks = self.selected_tracks()
        if not tracks:
            QMessageBox.information(self, "未选择文件", "请先选择一个音频文件。")
            return
        if len(tracks) > 1:
            QMessageBox.information(self, "请选择单个文件", "组合标签搜索一次针对一个文件。")
            return
        track = tracks[0]
        dialog = CombinedTagSearchDialog(
            self,
            track,
            self.track_query(track),
            selected_source_names(self.source_combo.currentText()),
        )
        dialog.exec()

    def apply_combined_tag(
        self,
        values: dict[str, str],
        overwrite_options: dict[str, bool],
        cover: bytes | None,
        cover_mime: str | None,
        missing_year: bool = False,
    ) -> None:
        if not self.current_tracks:
            return
        for key in ("title", "artist", "album", "year", "track", "disc", "genre", "comment"):
            if overwrite_options.get(key) and values.get(key, "").strip():
                self.field_edits[key].setText(values[key])
        if overwrite_options.get("lyrics") and values.get("lyrics", "").strip():
            self.lyrics_edit.setPlainText(values["lyrics"])
        if overwrite_options.get("cover") and cover:
            self.cover_bytes = cover
            self.cover_mime = cover_mime
            self.cover_dirty = True
            self._set_cover(cover, cover_mime)
        self.editor_dirty = True
        self.status_label.setText("组合标签已应用到编辑器，保存标签后写入文件")
        self.log("组合标签: 已应用候选到编辑器")
        if missing_year:
            self.log("组合标签: 候选没有发行年份，原程序会尝试查询网易云专辑信息")

    def track_query(self, track: Track) -> str:
        title = track.tags.get("title", "").strip()
        artist = track.tags.get("artist", "").strip()
        return f"{title} {artist}".strip() if title else track.path.stem

    def auto_match_lyrics(self) -> None:
        tracks = self.selected_tracks()
        if not tracks:
            QMessageBox.information(self, "未选择文件", "请先选择要自动匹配的文件。")
            return
        overwrite = self.overwrite_check.isChecked()
        pending = [track for track in tracks if overwrite or not track.lyrics.strip()]
        if not pending:
            QMessageBox.information(
                self,
                "无需处理",
                "所选文件都已有歌词。勾选“覆盖已有歌词”可重新匹配。",
            )
            return
        if QMessageBox.question(
            self,
            "自动匹配歌词",
            f"将自动搜索并写入 {len(pending)} 个文件。是否继续？",
        ) != QMessageBox.StandardButton.Yes:
            return

        source_names = selected_source_names(self.source_combo.currentText())
        self.auto_action.setEnabled(False)
        self.search_action.setEnabled(False)
        self.log(f"开始自动匹配: {len(pending)} 个文件")

        def work(progress):
            successes: list[tuple[Track, SearchHit]] = []
            failures: list[str] = []
            for index, track in enumerate(pending, start=1):
                progress(int((index - 1) / len(pending) * 100), f"正在匹配 {index}/{len(pending)}")
                query = self.track_query(track)
                try:
                    hits, errors = search_sources(query, source_names, logger=self.log_queue.put)
                    hit = best_hit(
                        hits,
                        track.tags.get("title", "") or track.path.stem,
                        track.tags.get("artist", ""),
                        track.tags.get("album", ""),
                    )
                    if hit is None or hit.score < 0.42:
                        failures.append(
                            f"{track.path.name}: {'; '.join(errors) if errors else '没有找到足够相似的歌曲'}"
                        )
                        continue
                    lyric = fetch_lyric(hit, logger=self.log_queue.put)
                    if not lyric.strip():
                        failures.append(f"{track.path.name}: 歌词为空")
                        continue
                    track.lyrics = lyric
                    save_track_tags(track, logger=self.log_queue.put)
                    successes.append((track, hit))
                except Exception as exc:
                    failures.append(f"{track.path.name}: {exc}")
            progress(100, "自动匹配完成")
            return successes, failures

        def done(result) -> None:
            successes, failures = result
            self.auto_action.setEnabled(True)
            self.search_action.setEnabled(True)
            self.track_model.refresh()
            self._on_selection_changed()
            for track, hit in successes:
                self.log(
                    f"匹配成功: {track.path.name} -> {hit.source} "
                    f"{hit.caption} ({hit.score:.2f})"
                )
            for failure in failures:
                self.log(f"匹配失败: {failure}")
            message = f"成功 {len(successes)} 个，失败 {len(failures)} 个。"
            if failures:
                message += "\n\n" + "\n".join(failures[:8])
            QMessageBox.information(self, "自动匹配结果", message)

        def error(exc: Exception) -> None:
            self.auto_action.setEnabled(True)
            self.search_action.setEnabled(True)
            self.show_error(exc)

        self.start_worker(work, done, error)

    def show_help(self) -> None:
        QMessageBox.information(
            self,
            "使用说明",
            "1. 打开音频文件或目录。\n"
            "2. 在左侧修改标签、封面或歌词。\n"
            "3. 使用“保存标签”写入文件。\n"
            "4. 使用“搜索歌词”手动选择歌词。\n"
            "5. 使用“自动匹配歌词”批量搜索并写入。\n\n"
            "支持 MP3、FLAC、M4A/MP4、OGG/Opus、WAV、AIFF、WMA。",
        )

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于",
            "音乐标签 Python\n\n"
            "功能等价的轻量 Python 实现。\n"
            "标签读写使用 mutagen，界面使用 PySide6。",
        )

    def has_unsaved_changes(self) -> bool:
        return self.editor_dirty or any(track.dirty for track in self.tracks)

    def closeEvent(self, event) -> None:
        if self.has_unsaved_changes():
            answer = QMessageBox.question(
                self,
                "未保存修改",
                "还有未写入文件的修改。仍要退出吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def main() -> None:
    app = QApplication([])
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    window = MusicTagApp()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
