@echo off
setlocal
title CortesViral
cd /d "%~dp0"
color 0B

REM ------------------------------------------------------------------
REM  Abre o CortesViral sem precisar gerar o executavel.
REM  Use este arquivo se voce ainda nao rodou o Gerar-Executavel.bat
REM  ou se o build deu problema.
REM ------------------------------------------------------------------

echo ============================================================
echo   CortesViral
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo   ERRO: Python nao encontrado.
    echo   Baixe em https://www.python.org/downloads/
    echo   IMPORTANTE: marque "Add Python to PATH" na instalacao.
    echo.
    pause
    exit /b 1
)

if not exist venv (
    echo   Primeira execucao: preparando o ambiente...
    echo   ^(isso demora alguns minutos, so acontece uma vez^)
    echo.
    python -m venv venv
    call venv\Scripts\activate.bat
    python -m pip install --upgrade pip --quiet
    pip install -r requirements.txt
    pip install -U yt-dlp --quiet
) else (
    call venv\Scripts\activate.bat
)

REM ffmpeg embutido na pasta bin, se existir
if exist bin\ffmpeg.exe set FFMPEG_BIN=%cd%\bin\ffmpeg.exe

echo.
echo   Abrindo o navegador em http://localhost:8000
echo   ^(deixe esta janela aberta enquanto usa o programa^)
echo.
python app.py
pause
