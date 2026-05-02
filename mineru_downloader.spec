# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — MinerU 批量解析下载工具
打包为单目录（onedir）可执行文件，保留 console 窗口（TUI/CLI 均需要）。
"""

import os
from pathlib import Path

block_cipher = None

# ── 项目根目录 ────────────────────────────────────────────────────────────────
ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # textual 内置主题 / 字体等（若有）
        (str(ROOT / '.venv/Lib/site-packages/textual'), 'textual'),
    ],
    hiddenimports=[
        # textual 动态导入的 widget 模块
        'textual',
        'textual.app',
        'textual.widgets',
        'textual.widgets._button',
        'textual.widgets._checkbox',
        'textual.widgets._footer',
        'textual.widgets._header',
        'textual.widgets._input',
        'textual.widgets._label',
        'textual.widgets._radio_button',
        'textual.widgets._radio_set',
        'textual.widgets._select',
        'textual.widgets._static',
        'textual.widgets._tree',
        'textual.widgets._list_view',
        'textual.widgets._list_item',
        'textual.widgets.tree',
        'textual.containers',
        'textual.binding',
        'textual.css.query',
        'textual.reactive',
        'textual.screen',
        'textual.color',
        'textual.driver',
        'textual._xterm_parser',
        'textual._text_area_theme',
        # rich（textual 依赖）
        'rich',
        'rich.console',
        'rich.live',
        'rich.panel',
        'rich.progress',
        'rich.table',
        'rich.tree',
        'rich.markup',
        'rich.syntax',
        # 标准库可能被遗漏的模块
        'concurrent.futures',
        'threading',
        'logging',
        'pathlib',
        'yaml',
        # urllib3 / requests 依赖的标准库（不能放进 excludes）
        'email',
        'email.message',
        'email.utils',
        'email.header',
        'email.encoders',
        'email.mime',
        'email.mime.multipart',
        'email.mime.text',
        'html',
        'html.parser',
        'http',
        'http.client',
        'http.cookiejar',
        # PDF 拆分
        'pypdf',
        'pypdf.generic',
        'pypdf._reader',
        'pypdf._writer',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'test',
        'distutils',
        'http.server',
        'xmlrpc',
        'pydoc',
    ],
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
    exclude_binaries=True,
    name='mineru-downloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,           # 必须保留 console，TUI/CLI 均依赖终端
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / 'logo.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='mineru-downloader',
)
