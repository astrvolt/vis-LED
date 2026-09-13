# -*- mode: python ; coding: utf-8 -*-
#
# Build with:  pyinstaller LEDPanel.spec
# Run this ON Windows to get the .exe, and ON macOS to get the .app —
# PyInstaller does not cross-compile, each OS's build only runs on that OS.
#
# Windows -> dist/LEDPanel/LEDPanel.exe  (ship the whole dist/LEDPanel folder)
# macOS   -> dist/LEDPanel.app           (self-contained bundle)
#
# icon.ico / icon.icns are optional — if you don't have them yet, delete
# the icon= lines below (or leave the files missing; the spec below
# already falls back to None if they're absent).

import os
import sys

block_cipher = None

_here = os.path.dirname(os.path.abspath(SPEC))
_ico = os.path.join(_here, 'icon.ico')
_icns = os.path.join(_here, 'icon.icns')

a = Analysis(
    ['panel_widget.py'],
    pathex=[],
    binaries=[],
    datas=[],
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
    [],
    exclude_binaries=True,
    name='LEDPanel',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,              # no terminal window on launch
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,     # fill in your Developer ID for macOS signing, see chat
    entitlements_file=None,
    icon=(_ico if sys.platform == 'win32' and os.path.exists(_ico)
          else (_icns if sys.platform == 'darwin' and os.path.exists(_icns) else None)),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LEDPanel',
)

# macOS-only: wraps the COLLECT output into an actual double-clickable
# LEDPanel.app with a proper Info.plist. PyInstaller ignores this block
# on Windows/Linux, so the same spec file works for both.
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='LEDPanel.app',
        icon=(_icns if os.path.exists(_icns) else None),
        bundle_identifier='com.astrvolt.ledpanel',
        info_plist={
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'NSHighResolutionCapable': True,
            # Required or macOS silently denies system-audio capture and
            # the app just gets silence with no error dialog explaining why.
            'NSMicrophoneUsageDescription': (
                'LED Panel reads system audio levels to drive its '
                'audio-reactive visualizer. No audio is recorded or sent anywhere.'
            ),
        },
    )
