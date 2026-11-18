# -*- mode: python ; coding: utf-8 -*-
"""onedir 打包：产出 dist/semg-desktop/semg-desktop.exe（exe + _internal 文件夹）。

构建：
    .venv/Scripts/python.exe -m PyInstaller semg_desktop.spec --noconfirm

录制与通道映射写在 exe 旁的 records/ 与 _internal/config/，整文件夹可拷贝分发。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)

# pyqtgraph 的 Qt 绑定、bleak 的 winrt 后端都是运行时才确定的导入，静态分析扫不全
hiddenimports = (
    collect_submodules("pyqtgraph")
    + collect_submodules("winrt")
    + [
        "bleak.backends.winrt",
        "bleak.backends.winrt.client",
        "bleak.backends.winrt.scanner",
        "serial.tools.list_ports_windows",
    ]
)

a = Analysis(
    [str(ROOT / "scripts" / "exe_main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(ROOT / "config" / "default.json"), "config")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="semg-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 子系统；selftest/parse 等子命令用退出码判断结果
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    [],
    name="semg-desktop",
    strip=False,
    upx=False,
)