# PyInstaller onedir/windowed distribution; no development or test payload.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
datas = [
    (str(root / 'backend/src/htd/web'), 'htd/web'),
    (str(root / 'backend/src/htd/adapters/sqlite/migrations'), 'htd/adapters/sqlite/migrations'),
    (str(root / 'packaging/app.ico'), 'htd/adapters/desktop/resources'),
    (str(root / 'packaging/THIRD_PARTY_NOTICES.md'), '.'),
    (str(root / 'LICENSE'), '.'),
]
datas += collect_data_files('webview', includes=['lib/**/*.dll'])
datas += collect_data_files('tzdata')
datas += copy_metadata('harness-token-deck')
a = Analysis(
    [str(root / 'backend/src/htd/__main__.py')],
    pathex=[str(root / 'backend/src')],
    hookspath=[str(root / 'packaging/hooks')],
    datas=datas,
    hiddenimports=['webview.platforms.winforms', 'webview.platforms.edgechromium',
                   'clr_loader', 'pythonnet', 'keyring.backends.Windows'],
    excludes=['tkinter', 'pytest', '_pytest', 'hypothesis', 'respx', 'psutil', 'IPython',
              'keyring.testing', 'pygments',
              'cffi._shimmed_dist_utils', 'cffi.setuptools_ext',
              'cffi.verifier', 'cffi.recompiler', 'cffi.ffiplatform',
              'cffi.vengine_cpy', 'cffi.vengine_gen', 'setuptools',
              'pydantic.v1._hypothesis_plugin',
              'numpy', 'pandas', 'matplotlib', 'PIL', 'gi', 'qtpy', 'PyQt5', 'PyQt6',
              'PySide2', 'PySide6', 'webview.platforms.gtk', 'webview.platforms.qt',
              'webview.platforms.cocoa', 'webview.platforms.android',
              'tests', 'fixtures', 'probes', 'htd.domain.common.testing'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='HarnessTokenDeck',
          debug=False, strip=False, upx=False, console=False,
          icon=str(root / 'packaging/app.ico'),
          version=str(root / 'build/version_info.py'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='HarnessTokenDeck')
