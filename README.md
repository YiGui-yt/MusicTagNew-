# MusicTag Python

简体中文版：[README.zh-CN.md](README.zh-CN.md)

This is a lightweight Python port of the core MusicTag workflow:

- read and edit common audio tags
- edit embedded lyrics manually or import an `.lrc` file
- preview and replace cover images
- search lyrics from NetEase Cloud Music or QQ Music
- automatically match and write lyrics for selected files
- batch apply changed editor values to multiple selected files

The UI is intentionally independent of the WinForms layout.

Combined tag search mirrors the original workflow: search candidates by
title/artist, inspect tags, lyrics and cover, then apply selected fields to
the editor. Overwrite choices are saved in `combined_tag_settings.json`.

## Requirements

- Windows 10 or newer
- Python 3.11 or newer
- `mutagen` for tag I/O
- `Pillow` for cover preview
- `PySide6` for the desktop UI

The repository environment was created with Python 3.13.

## Install

```powershell
py -m venv .\python-remake\.venv
.\python-remake\.venv\Scripts\python.exe -m pip install -r .\python-remake\requirements.txt
```

## Run

Double-click the repository-root launcher:

```text
Run-MusicTag-Python.bat
```

Or run from PowerShell:

```powershell
.\python-remake\run.ps1
```

Or directly:

```powershell
.\python-remake\.venv\Scripts\python.exe .\python-remake\app_qt.py
```

## Test

```powershell
.\python-remake\.venv\Scripts\python.exe .\python-remake\test_core.py
.\python-remake\.venv\Scripts\python.exe .\python-remake\test_ui.py
.\python-remake\.venv\Scripts\python.exe .\python-remake\test_combined_tag.py
```

## Supported formats

MP3, FLAC, M4A/MP4, OGG, Opus, WAV, AIFF, WMA/ASF.

## Intentional differences

- The UI is rebuilt with PySide6 and does not try to match WinForms pixel-for-pixel.
- ID3 handling uses mutagen directly. It does not reproduce every TagLibSharp version-selection rule.
- NetEase and QQ endpoints are public, unofficial interfaces and may change.
- The port currently focuses on tags, lyrics, covers, batch saving and automatic matching. Rename templates, history, undo and advanced DPI behavior are not included yet.
- Combined tag search applies candidate fields to the editor according to the
  saved overwrite options; the file is written only when “保存标签” is used.
- NetEase album details are used to fill a missing release year and cover when
  the search result does not carry them.
