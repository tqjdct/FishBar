# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_dir = Path(SPECPATH)
font_candidates = (
    project_dir / "NotoSerifSC-VF.ttf",
    Path("C:/Windows/Fonts/NotoSerifSC-VF.ttf"),
)
font_path = next((path for path in font_candidates if path.is_file()), None)
if font_path is None:
    raise FileNotFoundError(
        "缺少 NotoSerifSC-VF.ttf：请将字体放到项目目录，或安装到 C:/Windows/Fonts。"
    )

a = Analysis(
    [str(project_dir / "fishbar.py")],
    pathex=[],
    binaries=[],
    datas=[(str(font_path), "."), (str(project_dir / "NotoSerifSC-OFL.txt"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FishBar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
