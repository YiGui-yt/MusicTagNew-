# MusicTag Python 重制版

这是一个轻量级的 MusicTag 核心工作流 Python 移植版，支持：

- 读取和编辑常见音频标签
- 手动编辑内嵌歌词，或导入 `.lrc` 文件
- 预览、替换和移除封面图片
- 从网易云音乐或 QQ 音乐搜索歌词
- 为所选文件自动匹配并写入歌词
- 将编辑器中修改过的字段批量应用到多个所选文件

界面刻意保持独立，不复刻原 WinForms 布局。

组合标签搜索还原了原程序的工作流：按标题和艺术家搜索候选歌曲，查看标签、歌词和封面，再把所选字段应用到编辑器。覆盖选项保存在 `combined_tag_settings.json` 中。

## 环境要求

- Windows 10 或更高版本
- Python 3.11 或更高版本
- `mutagen`：用于标签读写
- `Pillow`：用于封面预览
- `PySide6`：用于桌面界面

仓库中的开发环境使用 Python 3.13 创建。

## 安装

```powershell
py -m venv .\python-remake\.venv
.\python-remake\.venv\Scripts\python.exe -m pip install -r .\python-remake\requirements.txt
```

## 运行

双击仓库根目录下的启动脚本：

```text
Run-MusicTag-Python.bat
```

也可以通过 PowerShell 启动：

```powershell
.\python-remake\run.ps1
```

或者直接运行：

```powershell
.\python-remake\.venv\Scripts\python.exe .\python-remake\app_qt.py
```

## 测试

```powershell
.\python-remake\.venv\Scripts\python.exe .\python-remake\test_core.py
.\python-remake\.venv\Scripts\python.exe .\python-remake\test_ui.py
.\python-remake\.venv\Scripts\python.exe .\python-remake\test_combined_tag.py
```

## 支持的格式

MP3、FLAC、M4A/MP4、OGG、Opus、WAV、AIFF、WMA/ASF。

## 有意保留的差异

- 界面使用 PySide6 重建，不追求与 WinForms 逐像素一致。
- ID3 处理直接使用 mutagen，不完整复刻 TagLibSharp 的所有版本选择规则。
- 网易云音乐和 QQ 音乐接口均为公开但非官方的接口，可能随时变化。
- 当前重制版聚焦标签、歌词、封面、批量保存和自动匹配，尚未包含重命名模板、历史记录、撤销和高级 DPI 行为。
- 组合标签搜索会按已保存的覆盖选项把候选字段应用到编辑器；只有点击“保存标签”后才会写入文件。
- 网易云搜索结果缺少发行年份或封面时，会通过专辑详情接口补齐。

## 打包版运行

已验证可运行的目录版发布包位于：

```text
python-remake\release\MusicTagPython-v1.0.0-win-x64-final.zip
```

解压后运行：

```text
MusicTagPython\MusicTagPython.exe
```

运行时必须保留 `MusicTagPython.exe` 与 `_internal` 目录的相对位置，不能只移动 EXE。
