from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('certifi'),
    hiddenimports=collect_submodules('Crypto.Cipher') + ['truststore._windows'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ULBPropertyScraper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
