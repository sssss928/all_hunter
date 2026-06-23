# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# PyInstaller Spec File for Tickets Hunter - NoDriver Version
# =============================================================================
# This spec file builds the NoDriver version of Tickets Hunter.
# Output: dist/nodriver_tixcraft/nodriver_tixcraft.exe
# =============================================================================

import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# Get the project root directory (parent of build_scripts)
project_root = os.path.abspath(os.path.join(SPECPATH, '..'))

# Collect ddddocr data files (including .onnx models)
ddddocr_datas = collect_data_files('ddddocr')

a = Analysis(
    [os.path.join(project_root, 'src', 'nodriver_tixcraft.py')],
    pathex=[os.path.join(project_root, 'src')],
    binaries=[],
    # Runtime resources are copied beside the executables by build_release.ps1.
    # Only package-owned data must live inside _internal.
    datas=ddddocr_datas,
    hiddenimports=[
        # Core dependencies
        'ddddocr',
        'onnxruntime',
        'onnxruntime.capi.onnxruntime_pybind11_state',
        'zendriver',
        'zendriver.cdp',
        'zendriver.core',
        # Shared utilities (important!)
        'util',
        'NonBrowser',
        'chrome_downloader',
        # Modular architecture
        'core',
        'core.observability',
        'core.retry',
        'core.state_machine',
        'dom',
        'dom.controller',
        'ocr',
        'ocr.pipeline',
        'verification',
        'verification.handoff_resume',
        'nodriver_common',
        'platforms',
        'platforms.registry',
        'platforms.facebook',
        'platforms.fansigo',
        'platforms.cityline',
        'platforms.famiticket',
        'platforms.ticketplus',
        'platforms.funone',
        'platforms.kktix',
        'platforms.tixcraft',
        'platforms.ibon',
        'platforms.kham',
        'platforms.hkticketing',
        'platforms.nolworld',
        # Chrome downloader dependencies
        'requests',
        # Image processing
        'PIL',
        'PIL.Image',
        'cv2',
        'numpy',
        # Network
        'urllib3',
        'certifi',
        'cryptography',
        # Others
        'playsound',
        'pyperclip',
        'tornado',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # This enables folder mode
    name='nodriver_tixcraft',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # Disable UPX compression for stability
    console=True,  # Show console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, 'src', 'www', 'favicon.ico'),  # Application icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='nodriver_tixcraft',
)
