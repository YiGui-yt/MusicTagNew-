# Legacy Tk prototype. Use app_qt.py through run.ps1.
from __future__ import annotations

import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from core import (
    FIELD_LABELS,
    PROVIDERS,
    SUPPORTED_EXTENSIONS,
    SearchHit,
    TagWriteError,
    Track,
    best_hit,
    fetch_lyric,
    format_duration,
    load_track,
    read_text_file,
    save_track_tags,
    score_hit,
    search_sources,
)


APP_TITLE = "音乐标签 Python"
BG = "#f3f6f8"
PANEL = "#ffffff"
INK = "#17212b"
MUTED = "#65727e"
ACCENT = "#0f766e"
ACCENT_LIGHT = "#d9f0ec"
BORDER = "#d7e0e5"


def selected_source_names(value: str) -> list[str]:
    if value == "网易云":
        return ["网易云"]
    if value == "QQ音乐":
        return ["QQ音乐"]
    return ["网易云", "QQ音乐"]


class MusicTagApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1440x860")
        self.minsize(1080, 680)
        self.configure(bg=BG)
        self.tracks: dict[str, Track] = {}
        self.hits: list[SearchHit] = []
        self.current_paths: list[str] = []
        self.editor_snapshot: dict[str, str] = {}
        self.cover_bytes: bytes | None = None
        self.cover_mime: str | None = None
        self.cover_dirty = False
        self.cover_photo: ImageTk.PhotoImage | None = None
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="musictag")
        self.source_var = tk.StringVar(value="网易云+QQ音乐")
        self.overwrite_lyrics_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")
        self.selection_var = tk.StringVar(value="未选择文件")
        self.progress_var = tk.DoubleVar(value=0)
        self.field_vars = {key: tk.StringVar() for key, _ in FIELD_LABELS}
        self._configure_style()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 9), background=BG, foreground=INK)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Toolbar.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Panel.TLabel", background=PANEL, foreground=INK)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 12, "bold"), background=PANEL)
        style.configure(
            "TButton",
            padding=(10, 6),
            background="#e9eff2",
            foreground=INK,
            borderwidth=0,
            focusthickness=0,
        )
        style.map("TButton", background=[("active", "#dce7eb"), ("disabled", "#edf1f3")])
        style.configure(
            "Accent.TButton",
            padding=(12, 6),
            background=ACCENT,
            foreground="#ffffff",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map("Accent.TButton", background=[("active", "#0b5f59"), ("disabled", "#9bbdb9")])
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER, padding=5)
        style.configure("TCombobox", fieldbackground="#ffffff", padding=4)
        style.configure(
            "Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground=INK,
            rowheight=27,
            bordercolor=BORDER,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background="#e8eef1",
            foreground=INK,
            relief="flat",
            padding=(6, 6),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map("Treeview", background=[("selected", "#cce9e4")], foreground=[("selected", INK)])
        style.configure("TProgressbar", background=ACCENT, troughcolor="#e5ecef", borderwidth=0)
        style.configure("TPanedwindow", background=BORDER)
        style.configure("TLabelframe", background=PANEL, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=PANEL, foreground=INK, font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TCheckbutton", background=PANEL, foreground=INK)

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="打开文件...", accelerator="Ctrl+O", command=self.open_files)
        file_menu.add_command(label="打开目录...", accelerator="Ctrl+Shift+O", command=self.open_directory)
        file_menu.add_separator()
        file_menu.add_command(label="保存标签", accelerator="Ctrl+S", command=self.save_selected)
        file_menu.add_command(label="导入 LRC...", command=self.import_lrc)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menu.add_cascade(label="文件", menu=file_menu)

        lyric_menu = tk.Menu(menu, tearoff=False)
        lyric_menu.add_command(label="搜索歌词...", accelerator="Ctrl+L", command=self.open_lyric_search)
        lyric_menu.add_command(label="自动匹配歌词", accelerator="Ctrl+Shift+L", command=self.auto_match_lyrics)
        lyric_menu.add_command(label="清空编辑器歌词", command=self.clear_editor_lyrics)
        menu.add_cascade(label="歌词", menu=lyric_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="选择封面...", command=self.select_cover)
        tools_menu.add_command(label="移除封面", command=self.remove_cover)
        tools_menu.add_separator()
        tools_menu.add_command(label="清空日志", command=self.clear_log)
        menu.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="使用说明", command=self.show_help)
        help_menu.add_command(label="关于", command=self.show_about)
        menu.add_cascade(label="帮助", menu=help_menu)
        self.config(menu=menu)

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 8))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="打开文件", command=self.open_files).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="打开目录", command=self.open_directory).pack(side=tk.LEFT, padx=(0, 6))
        self.save_button = ttk.Button(
            toolbar, text="保存标签", style="Accent.TButton", command=self.save_selected
        )
        self.save_button.pack(side=tk.LEFT, padx=(6, 16))
        ttk.Button(toolbar, text="搜索歌词", command=self.open_lyric_search).pack(side=tk.LEFT, padx=(0, 6))
        self.auto_button = ttk.Button(toolbar, text="自动匹配歌词", command=self.auto_match_lyrics)
        self.auto_button.pack(side=tk.LEFT, padx=(0, 16))
        ttk.Checkbutton(
            toolbar, text="覆盖已有歌词", variable=self.overwrite_lyrics_var
        ).pack(side=tk.LEFT)

        ttk.Label(toolbar, text="搜索来源", style="Panel.TLabel").pack(side=tk.RIGHT, padx=(8, 4))
        source_box = ttk.Combobox(
            toolbar,
            textvariable=self.source_var,
            values=("网易云+QQ音乐", "网易云", "QQ音乐"),
            state="readonly",
            width=16,
        )
        source_box.pack(side=tk.RIGHT)

    def _build_body(self) -> None:
        outer = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        editor_panel = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        right_panel = ttk.Panedwindow(outer, orient=tk.VERTICAL)
        outer.add(editor_panel, weight=0)
        outer.add(right_panel, weight=1)

        header = ttk.Frame(editor_panel, style="Panel.TFrame")
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text="标签编辑", style="Heading.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.selection_var, style="Muted.TLabel").pack(side=tk.RIGHT)

        fields = ttk.Frame(editor_panel, style="Panel.TFrame")
        fields.pack(fill=tk.X)
        for index, (key, label) in enumerate(FIELD_LABELS):
            row = index
            ttk.Label(fields, text=label, style="Panel.TLabel").grid(
                row=row, column=0, sticky="w", pady=(0, 5), padx=(0, 8)
            )
            ttk.Entry(fields, textvariable=self.field_vars[key]).grid(
                row=row, column=1, sticky="ew", pady=(0, 5)
            )
        fields.columnconfigure(1, weight=1)

        cover_frame = ttk.Frame(editor_panel, style="Panel.TFrame")
        cover_frame.pack(fill=tk.X, pady=(12, 8))
        self.cover_label = tk.Label(
            cover_frame,
            text="无封面",
            bg="#eef3f5",
            fg=MUTED,
            width=24,
            height=4,
            font=("Microsoft YaHei UI", 9),
        )
        self.cover_label.pack(side=tk.LEFT, padx=(0, 10))
        cover_actions = ttk.Frame(cover_frame, style="Panel.TFrame")
        cover_actions.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Button(cover_actions, text="选择封面", command=self.select_cover).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(cover_actions, text="移除封面", command=self.remove_cover).pack(fill=tk.X)

        lyrics_header = ttk.Frame(editor_panel, style="Panel.TFrame")
        lyrics_header.pack(fill=tk.X, pady=(4, 4))
        ttk.Label(lyrics_header, text="歌词", style="Panel.TLabel").pack(side=tk.LEFT)
        ttk.Button(lyrics_header, text="导入 LRC", command=self.import_lrc).pack(side=tk.RIGHT)
        self.lyrics_text = tk.Text(
            editor_panel,
            height=12,
            wrap=tk.NONE,
            undo=True,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            bg="#ffffff",
            fg=INK,
            insertbackground=INK,
            font=("Microsoft YaHei UI", 9),
            padx=8,
            pady=8,
        )
        self.lyrics_text.pack(fill=tk.BOTH, expand=True)

        table_frame = ttk.Frame(right_panel, style="Panel.TFrame", padding=10)
        log_frame = ttk.Frame(right_panel, style="Panel.TFrame", padding=(10, 0, 10, 10))
        right_panel.add(table_frame, weight=4)
        right_panel.add(log_frame, weight=1)

        columns = ("file", "title", "artist", "album", "year", "track", "duration", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "file": "文件名",
            "title": "标题",
            "artist": "艺术家",
            "album": "专辑",
            "year": "年份",
            "track": "音轨号",
            "duration": "时长",
            "status": "状态",
        }
        widths = {
            "file": 230,
            "title": 190,
            "artist": 170,
            "album": 190,
            "year": 70,
            "track": 70,
            "duration": 70,
            "status": 95,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=60, stretch=column in ("file", "title", "artist", "album"))
        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", lambda _event: self.open_lyric_search())
        self.tree.bind("<Delete>", lambda _event: self.remove_selected_files())
        self.tree.bind("<Button-3>", self._show_tree_menu)

        self.tree_menu = tk.Menu(self, tearoff=False)
        self.tree_menu.add_command(label="搜索歌词", command=self.open_lyric_search)
        self.tree_menu.add_command(label="自动匹配歌词", command=self.auto_match_lyrics)
        self.tree_menu.add_command(label="保存标签", command=self.save_selected)
        self.tree_menu.add_separator()
        self.tree_menu.add_command(label="从列表移除", command=self.remove_selected_files)

        ttk.Label(log_frame, text="运行日志", style="Panel.TLabel").pack(anchor="w", pady=(0, 4))
        self.log_text = tk.Text(
            log_frame,
            height=6,
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=0,
            bg="#f8fafb",
            fg=INK,
            font=("Microsoft YaHei UI", 9),
            padx=8,
            pady=6,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _build_statusbar(self) -> None:
        status = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 6))
        status.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(status, textvariable=self.status_var, style="Panel.TLabel").pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(
            status, orient=tk.HORIZONTAL, mode="determinate", variable=self.progress_var, length=220
        )
        self.progress.pack(side=tk.RIGHT)

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-o>", lambda _event: self.open_files())
        self.bind("<Control-Shift-O>", lambda _event: self.open_directory())
        self.bind("<Control-s>", lambda _event: self.save_selected())
        self.bind("<Control-l>", lambda _event: self.open_lyric_search())
        self.bind("<Control-Shift-L>", lambda _event: self.auto_match_lyrics())

    def _run_task(self, work, done, error=None) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                self.after(0, lambda captured=exc: (error or self._show_error)(captured))
                return
            self.after(0, lambda: done(result))

        threading.Thread(target=runner, daemon=True).start()

    def _show_error(self, exc: Exception) -> None:
        self.status_var.set("操作失败")
        self.progress_var.set(0)
        messagebox.showerror("操作失败", str(exc), parent=self)

    def set_status(self, text: str, progress: float | None = None) -> None:
        self.status_var.set(text)
        if progress is not None:
            self.progress_var.set(max(0, min(100, progress)))

    def log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def add_files(self, paths: list[str]) -> None:
        added = 0
        for raw_path in paths:
            path = str(Path(raw_path))
            if path in self.tracks:
                continue
            if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
                self.log(f"跳过不支持的文件: {path}")
                continue
            track = load_track(path)
            self.tracks[path] = track
            added += 1
            if track.load_error:
                self.log(f"读取失败: {Path(path).name}: {track.load_error}")
        self._refresh_tree()
        if added:
            self.set_status(f"已添加 {added} 个文件")

    def open_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self,
            title="选择音频文件",
            filetypes=[
                ("音频文件", "*.mp3 *.flac *.m4a *.mp4 *.ogg *.opus *.wav *.aiff *.wma"),
                ("所有文件", "*.*"),
            ],
        )
        if paths:
            self.add_files(list(paths))

    def open_directory(self) -> None:
        directory = filedialog.askdirectory(parent=self, title="选择音乐目录")
        if not directory:
            return
        self.set_status("正在扫描目录...")
        self.update_idletasks()
        paths = [
            str(path)
            for path in Path(directory).rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        self.add_files(paths)

    def _tree_values(self, track: Track) -> tuple[str, ...]:
        status = "读取失败" if track.load_error else ("有歌词" if track.lyrics else "无歌词")
        return (
            track.path.name,
            track.tags.get("title", ""),
            track.tags.get("artist", ""),
            track.tags.get("album", ""),
            track.tags.get("year", ""),
            track.tags.get("track", ""),
            format_duration(track.duration),
            status,
        )

    def _refresh_tree(self) -> None:
        selected = set(self.tree.selection())
        existing = set(self.tree.get_children(""))
        wanted = set(self.tracks)
        for item in existing - wanted:
            self.tree.delete(item)
        for path, track in self.tracks.items():
            values = self._tree_values(track)
            if path in existing:
                self.tree.item(path, values=values)
            else:
                self.tree.insert("", tk.END, iid=path, values=values)
        for path in selected:
            if path in wanted:
                self.tree.selection_add(path)

    def _selected_tracks(self) -> list[Track]:
        return [self.tracks[path] for path in self.tree.selection() if path in self.tracks]

    def _on_tree_select(self, _event=None) -> None:
        selected = self._selected_tracks()
        self.current_paths = [str(track.path) for track in selected]
        if not selected:
            self.selection_var.set("未选择文件")
            self._clear_editor()
            return
        if len(selected) == 1:
            track = selected[0]
            self.selection_var.set(track.path.name)
            self._load_single_track(track)
        else:
            self.selection_var.set(f"已选择 {len(selected)} 个文件")
            self._load_common_values(selected)
        self.set_status(f"已选择 {len(selected)} 个文件")

    def _clear_editor(self) -> None:
        for variable in self.field_vars.values():
            variable.set("")
        self.lyrics_text.delete("1.0", tk.END)
        self.editor_snapshot = {}
        self.cover_dirty = False
        self._set_cover(None, None)

    def _load_single_track(self, track: Track) -> None:
        for key, variable in self.field_vars.items():
            variable.set(track.tags.get(key, ""))
        self.lyrics_text.delete("1.0", tk.END)
        self.lyrics_text.insert("1.0", track.lyrics)
        self.editor_snapshot = track.snapshot()
        self.cover_dirty = False
        self._set_cover(track.cover, track.cover_mime)

    def _load_common_values(self, tracks: list[Track]) -> None:
        for key, variable in self.field_vars.items():
            values = {track.tags.get(key, "") for track in tracks}
            variable.set(next(iter(values)) if len(values) == 1 else "")
            self.editor_snapshot[key] = variable.get()
        lyric_values = {track.lyrics for track in tracks}
        self.lyrics_text.delete("1.0", tk.END)
        if len(lyric_values) == 1:
            self.lyrics_text.insert("1.0", next(iter(lyric_values)))
        self.cover_dirty = False
        self._set_cover(None, None)

    def _editor_values(self) -> dict[str, str]:
        return {key: variable.get().strip() for key, variable in self.field_vars.items()}

    def _editor_lyrics(self) -> str:
        return self.lyrics_text.get("1.0", tk.END).rstrip("\n")

    def _apply_editor_to_tracks(self, tracks: list[Track]) -> None:
        values = self._editor_values()
        lyrics = self._editor_lyrics()
        if len(tracks) == 1:
            tracks[0].tags.update(values)
            tracks[0].lyrics = lyrics
            if self.cover_dirty:
                tracks[0].cover = self.cover_bytes
                tracks[0].cover_mime = self.cover_mime
            return

        for track in tracks:
            for key, value in values.items():
                if value != self.editor_snapshot.get(key, ""):
                    track.tags[key] = value
            common_lyrics = self.lyrics_text.get("1.0", tk.END).rstrip("\n")
            if common_lyrics:
                first_lyrics = tracks[0].lyrics if tracks else ""
                if common_lyrics != first_lyrics:
                    track.lyrics = common_lyrics
            if self.cover_dirty:
                track.cover = self.cover_bytes
                track.cover_mime = self.cover_mime

    def save_selected(self) -> None:
        tracks = self._selected_tracks()
        if not tracks:
            messagebox.showwarning("未选择文件", "请先在列表中选择要保存的文件。", parent=self)
            return
        self._apply_editor_to_tracks(tracks)
        errors: list[str] = []
        for index, track in enumerate(tracks, start=1):
            try:
                save_track_tags(track)
                self.log(f"已保存: {track.path.name}")
            except TagWriteError as exc:
                errors.append(f"{track.path.name}: {exc}")
                self.log(f"保存失败: {track.path.name}: {exc}")
            self.set_status(f"正在保存 {index}/{len(tracks)}", index / len(tracks) * 100)
        self.cover_dirty = False
        self._refresh_tree()
        self._on_tree_select()
        self.set_status("保存完成" if not errors else "部分文件保存失败", 100)
        if errors:
            messagebox.showerror("保存失败", "\n".join(errors[:12]), parent=self)

    def import_lrc(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="导入歌词",
            filetypes=[("歌词文件", "*.lrc *.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            lyrics = read_text_file(path)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        self.lyrics_text.delete("1.0", tk.END)
        self.lyrics_text.insert("1.0", lyrics)
        self.log(f"已导入歌词: {Path(path).name}")

    def clear_editor_lyrics(self) -> None:
        self.lyrics_text.delete("1.0", tk.END)

    def select_cover(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="选择封面图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            suffix = Path(path).suffix.lower()
            mime = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
        except Exception as exc:
            messagebox.showerror("图片无效", str(exc), parent=self)
            return
        self.cover_dirty = True
        self._set_cover(data, mime)

    def remove_cover(self) -> None:
        self.cover_dirty = True
        self._set_cover(None, None)

    def _set_cover(self, data: bytes | None, mime: str | None) -> None:
        self.cover_bytes = data
        self.cover_mime = mime
        if not data:
            self.cover_photo = None
            self.cover_label.configure(image="", text="无封面")
            return
        try:
            with Image.open(io.BytesIO(data)) as image:
                preview = image.convert("RGB")
                preview.thumbnail((220, 180))
                self.cover_photo = ImageTk.PhotoImage(preview)
            self.cover_label.configure(image=self.cover_photo, text="")
        except Exception:
            self.cover_photo = None
            self.cover_label.configure(image="", text="封面无法预览")

    def _show_tree_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.tree_menu.tk_popup(event.x_root, event.y_root)

    def remove_selected_files(self) -> None:
        for path in list(self.tree.selection()):
            self.tree.delete(path)
            self.tracks.pop(path, None)
        self._on_tree_select()
        self._refresh_tree()

    def open_lyric_search(self) -> None:
        tracks = self._selected_tracks()
        if not tracks:
            messagebox.showwarning("未选择文件", "请先选择一个音频文件。", parent=self)
            return
        track = tracks[0]
        query = self._track_query(track)
        LyricSearchDialog(self, track, query, selected_source_names(self.source_var.get()))

    def _track_query(self, track: Track) -> str:
        title = track.tags.get("title", "").strip()
        artist = track.tags.get("artist", "").strip()
        if title:
            return f"{title} {artist}".strip()
        return track.path.stem

    def apply_lyric_to_editor(self, lyric: str) -> None:
        self.lyrics_text.delete("1.0", tk.END)
        self.lyrics_text.insert("1.0", lyric)

    def auto_match_lyrics(self) -> None:
        tracks = self._selected_tracks()
        if not tracks:
            messagebox.showwarning("未选择文件", "请先选择要自动匹配的文件。", parent=self)
            return
        overwrite = self.overwrite_lyrics_var.get()
        pending = [track for track in tracks if overwrite or not track.lyrics.strip()]
        if not pending:
            messagebox.showinfo("无需处理", "所选文件都已有歌词。勾选“覆盖已有歌词”可重新匹配。", parent=self)
            return
        if not messagebox.askyesno(
            "自动匹配歌词",
            f"将自动搜索并写入 {len(pending)} 个文件。是否继续？",
            parent=self,
        ):
            return

        source_names = selected_source_names(self.source_var.get())
        self.auto_button.state(["disabled"])
        self.set_status("正在自动匹配歌词...", 0)
        self.log(f"开始自动匹配: {len(pending)} 个文件")

        def work():
            successes: list[tuple[Track, SearchHit, str]] = []
            skipped: list[tuple[Track, SearchHit]] = []
            failures: list[str] = []
            for index, track in enumerate(pending, start=1):
                self.after(0, lambda value=index: self.set_status(f"正在匹配 {value}/{len(pending)}", value / len(pending) * 100))
                query = self._track_query(track)
                try:
                    hits, errors = search_sources(query, source_names)
                    hit = best_hit(
                        hits,
                        track.tags.get("title", "") or track.path.stem,
                        track.tags.get("artist", ""),
                        track.tags.get("album", ""),
                    )
                    if hit is None or hit.score < 0.42:
                        reason = "; ".join(errors) if errors else "没有找到足够相似的歌曲"
                        failures.append(f"{track.path.name}: {reason}")
                        continue
                    lyric = fetch_lyric(hit)
                    if not lyric.strip():
                        failures.append(f"{track.path.name}: 歌词为空")
                        continue
                    track.lyrics = lyric
                    save_track_tags(track)
                    successes.append((track, hit, lyric))
                except Exception as exc:
                    failures.append(f"{track.path.name}: {exc}")
            return successes, failures

        def done(result) -> None:
            successes, failures = result
            self.auto_button.state(["!disabled"])
            self._refresh_tree()
            self._on_tree_select()
            self.set_status("自动匹配完成", 100)
            for track, hit, _lyric in successes:
                self.log(f"匹配成功: {track.path.name} -> {hit.source} {hit.caption} ({hit.score:.2f})")
            for failure in failures:
                self.log(f"匹配失败: {failure}")
            message = f"成功 {len(successes)} 个，失败 {len(failures)} 个。"
            if failures:
                message += "\n\n" + "\n".join(failures[:8])
            messagebox.showinfo("自动匹配结果", message, parent=self)

        def error(exc: Exception) -> None:
            self.auto_button.state(["!disabled"])
            self._show_error(exc)

        self._run_task(work, done, error)

    def show_help(self) -> None:
        messagebox.showinfo(
            "使用说明",
            "1. 打开音频文件或目录。\n"
            "2. 在左侧修改标签、封面或歌词。\n"
            "3. 使用“保存标签”写入文件。\n"
            "4. 使用“搜索歌词”手动选择歌词。\n"
            "5. 使用“自动匹配歌词”批量搜索并写入。\n\n"
            "支持 MP3、FLAC、M4A/MP4、OGG/Opus、WAV、AIFF、WMA。",
            parent=self,
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            "音乐标签 Python\n\n"
            "功能等价的轻量 Python 实现。\n"
            "标签读写使用 mutagen，图片处理使用 Pillow。",
            parent=self,
        )

    def _editor_dirty(self) -> bool:
        tracks = self._selected_tracks()
        if not tracks:
            return False
        if len(tracks) == 1:
            track = tracks[0]
            values = self._editor_values()
            if any(values[key] != track.tags.get(key, "") for key in values):
                return True
            if self._editor_lyrics() != track.lyrics:
                return True
            if self.cover_dirty:
                return True
            return False
        values = self._editor_values()
        return any(value != self.editor_snapshot.get(key, "") for key, value in values.items()) or self.cover_dirty

    def _on_close(self) -> None:
        if self._editor_dirty() and not messagebox.askyesno(
            "未保存修改", "当前编辑内容尚未保存，仍要退出吗？", parent=self
        ):
            return
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


class LyricSearchDialog(tk.Toplevel):
    def __init__(self, app: MusicTagApp, track: Track, query: str, source_names: list[str]) -> None:
        super().__init__(app)
        self.app = app
        self.track = track
        self.source_names = source_names
        self.hits: list[SearchHit] = []
        self.title("搜索歌词")
        self.geometry("920x620")
        self.minsize(780, 520)
        self.configure(bg=BG)
        self.transient(app)
        self._build(query)
        self.grab_set()

    def _build(self, query: str) -> None:
        top = ttk.Frame(self, style="Panel.TFrame", padding=12)
        top.pack(fill=tk.X)
        ttk.Label(top, text="搜索", style="Panel.TLabel").pack(side=tk.LEFT)
        self.query_var = tk.StringVar(value=query)
        entry = ttk.Entry(top, textvariable=self.query_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        entry.bind("<Return>", lambda _event: self.search())
        ttk.Button(top, text="搜索", style="Accent.TButton", command=self.search).pack(side=tk.LEFT)

        ttk.Label(
            top,
            text=f"来源: {'/'.join(self.source_names)}",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(12, 0))

        body = ttk.Panedwindow(self, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        result_frame = ttk.Frame(body, style="Panel.TFrame")
        preview_frame = ttk.Frame(body, style="Panel.TFrame")
        body.add(result_frame, weight=3)
        body.add(preview_frame, weight=2)

        columns = ("source", "title", "artist", "album", "duration", "score")
        self.result_tree = ttk.Treeview(result_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "source": "来源",
            "title": "歌曲",
            "artist": "艺术家",
            "album": "专辑",
            "duration": "时长",
            "score": "匹配度",
        }
        widths = {"source": 90, "title": 230, "artist": 180, "album": 220, "duration": 80, "score": 70}
        for column in columns:
            self.result_tree.heading(column, text=headings[column])
            self.result_tree.column(column, width=widths[column], stretch=column in ("title", "artist", "album"))
        scrollbar = ttk.Scrollbar(result_frame, orient=tk.VERTICAL, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=scrollbar.set)
        self.result_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_tree.bind("<Double-1>", lambda _event: self.use_lyric(save=False))

        ttk.Label(preview_frame, text="歌词预览", style="Panel.TLabel").pack(anchor="w", pady=(8, 4))
        self.preview_text = tk.Text(
            preview_frame,
            wrap=tk.NONE,
            relief=tk.FLAT,
            bg="#f8fafb",
            fg=INK,
            font=("Microsoft YaHei UI", 9),
            padx=8,
            pady=6,
        )
        self.preview_text.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self, style="Panel.TFrame", padding=(12, 0, 12, 12))
        footer.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="输入关键词后搜索")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(footer, text="加载预览", command=lambda: self.use_lyric(save=False)).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(footer, text="应用并保存", style="Accent.TButton", command=lambda: self.use_lyric(save=True)).pack(side=tk.RIGHT)

    def search(self) -> None:
        query = self.query_var.get().strip()
        if not query:
            self.status_var.set("请输入搜索关键词")
            return
        self.status_var.set("正在搜索...")

        def work():
            return search_sources(query, self.source_names)

        def done(result) -> None:
            hits, errors = result
            self.hits = hits
            for hit in self.hits:
                hit.score = score_hit(
                    hit,
                    self.track.tags.get("title", "") or self.track.path.stem,
                    self.track.tags.get("artist", ""),
                    self.track.tags.get("album", ""),
                )
            self.hits.sort(key=lambda item: item.score, reverse=True)
            for item in self.result_tree.get_children(""):
                self.result_tree.delete(item)
            for index, hit in enumerate(self.hits):
                self.result_tree.insert(
                    "",
                    tk.END,
                    iid=str(index),
                    values=(
                        hit.source,
                        hit.title,
                        hit.artist,
                        hit.album,
                        format_duration(hit.duration),
                        f"{hit.score:.0%}",
                    ),
                )
            if self.hits:
                message = f"找到 {len(self.hits)} 个候选"
                if errors:
                    message += f"，部分来源失败: {'; '.join(errors)}"
            else:
                message = "没有找到结果"
                if errors:
                    message += ": " + "; ".join(errors)
            self.status_var.set(message)

        def error(exc: Exception) -> None:
            self.status_var.set("搜索失败")
            messagebox.showerror("搜索失败", str(exc), parent=self)

        self.app._run_task(work, done, error)

    def _selected_hit(self) -> SearchHit | None:
        selection = self.result_tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return self.hits[index] if 0 <= index < len(self.hits) else None

    def _load_lyric(self) -> tuple[SearchHit, str] | None:
        hit = self._selected_hit()
        if hit is None:
            messagebox.showwarning("未选择结果", "请先选择一个歌词候选。", parent=self)
            return None
        return hit, fetch_lyric(hit)

    def use_lyric(self, save: bool) -> None:
        hit = self._selected_hit()
        if hit is None:
            messagebox.showwarning("未选择结果", "请先选择一个歌词候选。", parent=self)
            return
        self.status_var.set(f"正在加载 {hit.source} 歌词...")

        def work():
            return fetch_lyric(hit)

        def done(lyric: str) -> None:
            self.preview_text.delete("1.0", tk.END)
            self.preview_text.insert("1.0", lyric)
            self.app.apply_lyric_to_editor(lyric)
            self.status_var.set(f"已加载: {hit.source} {hit.caption}")
            self.app.log(f"歌词预览: {hit.source} {hit.caption}")
            if save:
                self.track.lyrics = lyric
                save_track_tags(self.track)
                self.app._refresh_tree()
                self.app._on_tree_select()
                self.app.log(f"歌词已写入: {self.track.path.name}")
                self.status_var.set("歌词已写入并保存")

        def error(exc: Exception) -> None:
            self.status_var.set("歌词加载失败")
            messagebox.showerror("歌词加载失败", str(exc), parent=self)

        self.app._run_task(work, done, error)


def main() -> None:
    app = MusicTagApp()
    app.mainloop()


if __name__ == "__main__":
    main()
