# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for 3Cat Downloader (Windows, one-folder, windowed).

Build from the project root:

    .venv\\Scripts\\pyinstaller.exe 3CatDownloader.spec --noconfirm --clean

Result: dist\\3CatDownloader\\3CatDownloader.exe (+ _internal\\ with the
Python runtime, the web UI and the bundled ffmpeg binary).
"""
from pathlib import Path

import imageio_ffmpeg
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)  # noqa: F821 - injected by PyInstaller
APP_NAME = "3CatDownloader"

# Static UI, found at runtime under sys._MEIPASS/tresdl/web (tresdl.server.resolve_web_dir).
datas = [(str(ROOT / "tresdl" / "web"), "tresdl/web")]

# ffmpeg from imageio-ffmpeg, at the place imageio_ffmpeg.get_ffmpeg_exe() looks for
# it (imageio_ffmpeg/binaries). The contrib hook also collects it; listing it here
# makes the requirement explicit and fails the build early if it is missing.
ffmpeg_exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
if not ffmpeg_exe.is_file():
    raise SystemExit(f"ffmpeg not found: {ffmpeg_exe}")
datas.append((str(ffmpeg_exe), "imageio_ffmpeg/binaries"))

hiddenimports = (
    collect_submodules("tresdl")      # sibling modules are imported lazily
    + collect_submodules("uvicorn")   # uvicorn picks loop/protocol modules by name
    + ["imageio_ffmpeg.binaries", "python_multipart", "multipart"]
)

icon_candidates = [ROOT / "tresdl" / "web" / "icon.ico", ROOT / "assets" / "icon.ico"]
icon = next((str(p) for p in icon_candidates if p.is_file()), None)

a = Analysis(  # noqa: F821
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest", "PyInstaller"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # one-folder build
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX-packed exes trigger antivirus false positives
    console=False,              # windowed: no console window
    disable_windowed_traceback=False,
    icon=icon,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
