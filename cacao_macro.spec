# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('customtkinter')

# 관리자 모드에서 발급한 빌드용 라이선스 파일을 exe에 포함시킨다.
# (관리자 모드 > 빌드용 파일 생성으로 먼저 만들어 두어야 한다. 없으면 빌드 시 오류.)
if not os.path.exists('license_build.json'):
    raise FileNotFoundError(
        "license_build.json이 없습니다. 먼저 프로그램의 관리자 모드에서 "
        "사용 기간을 정해 빌드용 파일을 생성한 뒤 다시 빌드하세요."
    )
datas += [('license_build.json', '.')]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='cacao_macro',
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
