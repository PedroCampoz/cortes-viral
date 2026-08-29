# -*- mode: python ; coding: utf-8 -*-
"""
Receita do PyInstaller para gerar o CortesViral.exe (Windows).

Uso (no Windows, dentro da pasta do projeto):
    pip install pyinstaller
    pyinstaller CortesViral.spec

O resultado sai em dist\\CortesViral\\CortesViral.exe
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("static", "static"),      # interface web
    ("fonts", "fonts"),        # fontes Sora
]
binaries = []
hiddenimports = []

# Bibliotecas que carregam coisas dinamicamente e precisam ser levadas inteiras
for pacote in ("faster_whisper", "ctranslate2", "tokenizers", "onnxruntime",
               "av", "yt_dlp", "googleapiclient", "google_auth_oauthlib",
               "google.auth", "charset_normalizer"):
    try:
        d, b, h = collect_all(pacote)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass   # pacote opcional ausente não impede o build

hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "encodings.idna", "multipart", "python_multipart",
]

# Se você colocar o ffmpeg.exe em bin\ ele vai junto no executável
import os
if os.path.isfile(os.path.join("bin", "ffmpeg.exe")):
    binaries += [(os.path.join("bin", "ffmpeg.exe"), "bin")]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CortesViral",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # mantém a janela preta: mostra o progresso e erros
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CortesViral",
)
