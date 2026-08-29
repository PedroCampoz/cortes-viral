#!/bin/bash
# CortesViral — iniciador de duplo clique (macOS)
cd "$(dirname "$0")"
clear
echo "✂️  CortesViral — preparando ambiente..."

# 1. Localizar um ffmpeg COM suporte a legendas embutidas (libass)
ffmpeg_com_legenda() { [ -x "$1" ] && "$1" -filters 2>/dev/null | grep -qE '^ *[A-Z.]+ +ass +'; }

FFMPEG_BIN=""
for c in "$PWD/bin/ffmpeg" /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg "$(command -v ffmpeg 2>/dev/null)"; do
  if ffmpeg_com_legenda "$c"; then FFMPEG_BIN="$c"; break; fi
done

if [ -z "$FFMPEG_BIN" ]; then
  echo "→ Seu ffmpeg não suporta legendas embutidas. Instalando versão completa..."
  if command -v brew >/dev/null 2>&1; then
    brew install ffmpeg 2>/dev/null || brew upgrade ffmpeg
    for c in /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg; do
      if ffmpeg_com_legenda "$c"; then FFMPEG_BIN="$c"; break; fi
    done
  fi
  if [ -z "$FFMPEG_BIN" ]; then
    echo "→ Baixando ffmpeg estático (evermeet.cx)..."
    mkdir -p bin
    curl -L --progress-bar -o bin/ffmpeg.zip "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" \
      && (cd bin && unzip -o -q ffmpeg.zip && rm -f ffmpeg.zip && chmod +x ffmpeg)
    if ffmpeg_com_legenda "$PWD/bin/ffmpeg"; then FFMPEG_BIN="$PWD/bin/ffmpeg"; fi
  fi
fi

if [ -z "$FFMPEG_BIN" ]; then
  echo "❌ Não consegui obter um ffmpeg com suporte a legendas."
  echo "   Instale o Homebrew (https://brew.sh) e rode este arquivo de novo."
  read -p "Pressione Enter para fechar..."
  exit 1
fi
export FFMPEG_BIN
echo "→ Usando ffmpeg: $FFMPEG_BIN"

# 2. Ambiente Python (prefere a versão mais nova disponível)
PY=$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)
if [ ! -d venv ]; then
  echo "→ Criando ambiente Python ($($PY --version))..."
  "$PY" -m venv venv
fi
source venv/bin/activate
echo "→ Instalando dependências (primeira vez pode demorar ~2 min)..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
# yt-dlp precisa estar sempre atualizado (YouTube muda o protocolo com frequência)
echo "→ Atualizando yt-dlp..."
pip install -q -U yt-dlp

# 3. Encerrar instância antiga (garante que o código novo entre no ar)
OLD=$(lsof -ti:8000 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "→ Encerrando servidor antigo..."
  kill -9 $OLD 2>/dev/null
  sleep 1
fi

# 4. Abrir navegador e iniciar servidor
( sleep 3 && open "http://localhost:8000" ) &
echo ""
echo "✅ Pronto! Abrindo http://localhost:8000"
echo "   Cole o link do YouTube e clique em 'Gerar cortes virais'."
echo "   (Deixe esta janela aberta enquanto usa a plataforma)"
echo ""
exec venv/bin/uvicorn app:app --port 8000
