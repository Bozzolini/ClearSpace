# PyInstaller spec for the Windows 10/11 distributable (one-folder build: fast start, no temp extraction).
# Build with:  build_dist.bat   (or: python -m PyInstaller --noconfirm ClearSpace.spec)
from PyInstaller.utils.hooks import collect_data_files

datas = [
    ("app/i18n/*.json", "app/i18n"),
    ("assets/ClearSpaceIcon3.ico", "assets"),
]
datas += collect_data_files("customtkinter")

a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=["xxhash", "send2trash.win"],
    excludes=["pytest", "PyInstaller", "numpy", "PIL.ImageQt", "PySide6", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClearSpace",
    icon="assets/ClearSpaceIcon3.ico",
    version="version_info.txt",
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ClearSpace")
