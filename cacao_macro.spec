# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('customtkinter')

# License Externalization Sprint: license_build.json을 더 이상 datas로 exe
# 내부에 포함시키지 않는다 — exe와 같은 폴더에 두는 외부 파일이다
# (core/license_manager.py의 get_external_license_path() 참고). 이렇게 해야
# 라이선스를 갱신할 때 이 exe를 재빌드하지 않고 파일 교체만으로 반영된다.
# 배포 시에는 dist/ 폴더에 cacao_macro.exe와 함께 .env, license_build.json을
# 수동으로 배치해야 한다(docs/license_deployment_guide.md 참고).


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
