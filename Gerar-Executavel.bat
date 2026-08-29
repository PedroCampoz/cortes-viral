@echo off
setlocal enabledelayedexpansion
title CortesViral - Gerar executavel
cd /d "%~dp0"
color 0B

echo ============================================================
echo   CortesViral - gerando o executavel para Windows
echo ============================================================
echo.
echo   Isto roda UMA VEZ e demora de 10 a 25 minutos.
echo   No final voce tera a pasta:  dist\CortesViral\
echo   Dentro dela, o arquivo CortesViral.exe abre o programa.
echo.
pause

REM ---------- 1. Verificar Python ----------
echo.
echo [1/5] Procurando Python...
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERRO: Python nao encontrado.
    echo   Baixe em https://www.python.org/downloads/
    echo   IMPORTANTE: marque "Add Python to PATH" durante a instalacao.
    echo.
    pause
    exit /b 1
)
python --version

REM ---------- 2. Ambiente virtual ----------
echo.
echo [2/5] Preparando ambiente Python...
if not exist venv (
    python -m venv venv
)
call venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet

REM ---------- 3. Dependencias ----------
echo.
echo [3/5] Instalando dependencias (parte mais demorada)...
pip install -r requirements.txt --quiet
pip install pyinstaller --quiet
pip install -U yt-dlp --quiet

REM ---------- 4. ffmpeg ----------
echo.
echo [4/5] Verificando ffmpeg...
if exist bin\ffmpeg.exe (
    echo   ffmpeg ja esta em bin\ - sera embutido no executavel.
) else (
    echo   Baixando ffmpeg ^(cerca de 100 MB^)...
    if not exist bin mkdir bin
    powershell -NoProfile -Command ^
      "$ErrorActionPreference='Stop';" ^
      "try {" ^
      "  Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'bin\ffmpeg.zip';" ^
      "  Expand-Archive -Path 'bin\ffmpeg.zip' -DestinationPath 'bin\tmp' -Force;" ^
      "  $exe = Get-ChildItem -Path 'bin\tmp' -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1;" ^
      "  Copy-Item $exe.FullName 'bin\ffmpeg.exe' -Force;" ^
      "  $pb = Get-ChildItem -Path 'bin\tmp' -Recurse -Filter 'ffprobe.exe' | Select-Object -First 1;" ^
      "  if ($pb) { Copy-Item $pb.FullName 'bin\ffprobe.exe' -Force }" ^
      "  Remove-Item 'bin\ffmpeg.zip','bin\tmp' -Recurse -Force;" ^
      "  Write-Host '  ffmpeg baixado com sucesso.'" ^
      "} catch { Write-Host '  AVISO: nao consegui baixar o ffmpeg automaticamente.' }"
)
if not exist bin\ffmpeg.exe (
    echo.
    echo   AVISO: sem ffmpeg embutido. O programa vai funcionar mesmo assim
    echo   SE voce instalar o ffmpeg separado ^(winget install ffmpeg^).
    echo.
)

REM ---------- 5. Build ----------
echo.
echo [5/5] Gerando o executavel... ^(pode demorar bastante, e normal^)
pyinstaller CortesViral.spec --noconfirm --clean

echo.
if exist dist\CortesViral\CortesViral.exe (
    color 0A
    echo ============================================================
    echo   PRONTO!
    echo ============================================================
    echo.
    echo   O programa esta em:  dist\CortesViral\
    echo   Para usar: entre nessa pasta e clique 2x em CortesViral.exe
    echo.
    echo   Voce pode copiar a pasta "CortesViral" inteira para onde
    echo   quiser ^(area de trabalho, pen drive, outro PC^).
    echo   Seus cortes ficarao salvos em CortesViral-dados\clips
    echo.
    explorer dist\CortesViral
) else (
    color 0C
    echo ============================================================
    echo   Algo deu errado - o executavel nao foi gerado.
    echo   Role a tela para cima e veja a mensagem de erro.
    echo ============================================================
)
echo.
pause
