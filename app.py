"""
CortesViral — Plataforma local para gerar cortes virais de vídeos longos.

Pipeline: yt-dlp (download HD) → faster-whisper (transcrição com timestamps
por palavra) → Claude API (identifica hook / desenvolvimento / CTA) →
ffmpeg (corte 9:16 1080x1920 com legenda queimada palavra-por-palavra + .srt)

Rodar:  uvicorn app:app --port 8000   →  abrir http://localhost:8000
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# publicação no YouTube (opcional — só é usada se o usuário configurar)
try:
    from googleapiclient.discovery import build as yt_build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials as GoogleCredentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request as GoogleAuthRequest
    YT_LIBS_OK = True
except Exception:
    YT_LIBS_OK = False

# --------------------------------------------------------------------------
# Caminhos — funciona rodando o .py OU empacotado como .exe (PyInstaller)
# --------------------------------------------------------------------------
# RES  = recursos só de leitura (static/, fontes que vêm junto)
# BASE = dados do usuário (cortes, projetos, config) — sempre uma pasta normal,
#        ao lado do executável, para o usuário achar os vídeos com facilidade.

EMPACOTADO = getattr(sys, "frozen", False)
if EMPACOTADO:
    RES = Path(sys._MEIPASS)                                   # type: ignore[attr-defined]
    BASE = Path(sys.executable).parent / "CortesViral-dados"
else:
    RES = Path(__file__).parent
    BASE = Path(__file__).parent
BASE.mkdir(parents=True, exist_ok=True)

WORK = BASE / "workspace"          # vídeos baixados + transcrições
CLIPS = BASE / "clips"             # cortes finais
FONTS = BASE / "fonts"             # fontes enviadas pelo usuário (.ttf/.otf)
MUSIC = BASE / "music"             # trilhas enviadas pelo usuário
YOUTUBE_DIR = BASE / "youtube"     # credenciais de publicação no YouTube
for _d in (WORK, CLIPS, FONTS, MUSIC, YOUTUBE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# fontes que vêm dentro do executável (Sora) são copiadas na primeira execução
if EMPACOTADO and (RES / "fonts").is_dir():
    for _f in (RES / "fonts").glob("*"):
        if _f.is_file() and not (FONTS / _f.name).exists():
            try:
                shutil.copy2(_f, FONTS / _f.name)
            except Exception:
                pass


def achar_ffmpeg():
    """Localiza o ffmpeg: variável de ambiente, pasta do app, PATH ou locais
    comuns de instalação (Windows/macOS/Linux)."""
    env = os.environ.get("FFMPEG_BIN")
    if env and (Path(env).is_file() or shutil.which(env)):
        return env
    win = platform.system() == "Windows"
    nome = "ffmpeg.exe" if win else "ffmpeg"
    # 1) ao lado do executável/script, em bin/
    for raiz in {Path(sys.executable).parent, BASE, RES}:
        c = raiz / "bin" / nome
        if c.is_file():
            return str(c)
    # 2) no PATH
    achado = shutil.which("ffmpeg")
    if achado:
        return achado
    # 3) locais comuns
    candidatos = ([r"C:\ffmpeg\bin\ffmpeg.exe",
                   r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                   os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
                   r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"] if win else
                  ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                   "/usr/bin/ffmpeg"])
    for c in candidatos:
        if c and Path(c).is_file():
            return c
    return "ffmpeg"          # última tentativa: deixa o sistema resolver


FFMPEG = achar_ffmpeg()

VERSAO = "2026.08.18"          # aparece na tela: confirma que o build é o novo

app = FastAPI(title="CortesViral")
JOBS: dict = {}                    # job_id -> estado (em memória)

# fonte customizada global (aplicada à legenda e à tarja)
CUSTOM_FONT = {"path": None, "family": None}

# música de fundo global (aplicada a todos os cortes)
BG_MUSIC = {"path": None, "name": None}


def font_family():
    """Nome da família da fonte a usar no ASS (custom ou padrão)."""
    return CUSTOM_FONT["family"] or "Arial Black"


def ffmpeg_hint():
    """Dica de instalação do ffmpeg de acordo com o sistema operacional."""
    s = platform.system()
    if s == "Windows":
        return "winget install ffmpeg  (ou choco install ffmpeg)"
    if s == "Darwin":
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg  (ou o gerenciador de pacotes da sua distro)"


# --------------------------------------------------------------------------
# Configuração salva (chave de API padrão, motor, modelo)
# --------------------------------------------------------------------------

CONFIG_FILE = BASE / "config.json"
CONFIG = {"api_key": "", "engine_choice": "gemini",
          "gemini_model": "gemini-2.5-flash", "ollama_model": "",
          "openai_model": "gpt-5.6-terra",
          "template": "sora-clean", "music_folder": ""}


def load_config():
    try:
        if CONFIG_FILE.exists():
            CONFIG.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False),
                               encoding="utf-8")
    except Exception:
        pass


load_config()


def chave_efetiva(job):
    """Chave do job ou, se vazia, a chave padrão salva no app."""
    return (job.get("api_key") or CONFIG.get("api_key") or "").strip()


# --------------------------------------------------------------------------
# Persistência dos projetos (para o editor funcionar após reiniciar)
# --------------------------------------------------------------------------

def save_job(job):
    """Grava o estado do projeto em disco (sem a chave de API)."""
    try:
        d = {k: v for k, v in job.items() if k != "api_key"}
        d["_render"] = {str(k): v for k, v in job.get("_render", {}).items()}
        p = WORK / job["id"]
        p.mkdir(parents=True, exist_ok=True)
        (p / "job.json").write_text(json.dumps(d, ensure_ascii=False),
                                    encoding="utf-8")
    except Exception:
        pass


def load_jobs():
    """Recarrega os projetos salvos ao iniciar o servidor."""
    for f in WORK.glob("*/job.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            d["_render"] = {int(k): v for k, v in d.get("_render", {}).items()}
            JOBS[d["id"]] = d
        except Exception:
            continue


load_jobs()

# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------

class JobRequest(BaseModel):
    url: str = ""                  # link YouTube (vazio quando é upload)
    api_key: Optional[str] = None  # chave Anthropic (opcional -> heurística)
    num_clips: int = 3
    min_len: int = 25              # duração mínima do corte (s)
    max_len: int = 75              # duração máxima do corte (s)
    whisper_model: str = "small"   # tiny/base/small/medium/large-v3
    layout: str = "split"          # split = imagem + tarja + vídeo | full = tela cheia
    engine_choice: str = "auto"    # auto | heuristica | ollama | gemini | openai | api
    ollama_model: str = ""         # ex: llama3.1 (vazio = padrão do servidor)
    gemini_model: str = ""         # ex: gemini-2.5-flash (vazio = padrão)
    openai_model: str = ""         # ex: gpt-5.6-terra (vazio = padrão)
    music_volume: int = 12         # volume da música de fundo (0-100 %)
    music_folder: Optional[str] = ""  # pasta com várias trilhas (rodízio por corte)
    template: str = "sora-clean"   # modelo visual da legenda


def set_status(job, step, progress, detail=""):
    job["step"] = step
    job["progress"] = progress
    job["detail"] = detail


# --------------------------------------------------------------------------
# 1. Download (yt-dlp)
# --------------------------------------------------------------------------

def download_video(job):
    out = WORK / job["id"] / "video.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    base = ["yt-dlp", "--merge-output-format", "mp4", "-o", str(out),
            "--no-playlist", "--force-overwrites", "--retries", "5",
            "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "-S", "res:1080"]                     # prioriza 1080p sempre
    # YouTube muda de protocolo com frequência (ex.: SABR); tenta vários clientes
    strategies = [
        [],
        ["--extractor-args", "youtube:player_client=android"],
        ["--extractor-args", "youtube:player_client=ios"],
        ["--extractor-args", "youtube:player_client=tv"],
    ]
    last_err = None
    for i, extra in enumerate(strategies, 1):
        try:
            set_status(job, "baixando", 5 + i,
                       f"Baixando vídeo HD (tentativa {i}/{len(strategies)})")
            subprocess.run(base + extra + [job["url"]], check=True,
                           capture_output=True, text=True)
            last_err = None
            break
        except subprocess.CalledProcessError as e:
            last_err = e
    if last_err or not out.exists():
        raise last_err or RuntimeError("Download falhou: arquivo não gerado.")
    # título do vídeo
    r = subprocess.run(["yt-dlp", "--get-title", "--no-playlist", job["url"]],
                       capture_output=True, text=True)
    job["video_title"] = r.stdout.strip() or "video"
    return out


# --------------------------------------------------------------------------
# 2. Transcrição (faster-whisper, timestamps por palavra)
# --------------------------------------------------------------------------

def transcribe(job, video_path):
    from faster_whisper import WhisperModel
    model = WhisperModel(job["whisper_model"], device="auto", compute_type="auto")
    segments, info = model.transcribe(str(video_path), word_timestamps=True,
                                      vad_filter=True)
    words, text_parts = [], []
    for seg in segments:
        text_parts.append(seg.text)
        for w in seg.words or []:
            words.append({"w": w.word.strip(), "s": round(w.start, 3),
                          "e": round(w.end, 3)})
        set_status(job, "transcrevendo", 30 + min(30, int(seg.end / 60)),
                   f"{seg.end:.0f}s transcritos")
    job["language"] = info.language
    transcript = {"words": words, "text": " ".join(text_parts).strip()}
    (WORK / job["id"] / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    return transcript


# --------------------------------------------------------------------------
# 3. Análise viral — Claude API (fallback: heurística local)
# --------------------------------------------------------------------------

ANALYSIS_PROMPT = """Você é um estrategista de conteúdo viral especialista em cortes \
para Reels/TikTok/Shorts. Analise a transcrição abaixo (com timestamps em segundos) \
e escolha os {n} melhores trechos para cortes virais.

REGRA DE DURAÇÃO (OBRIGATÓRIA): cada corte deve ter entre {min_len} e {max_len} \
segundos, ou seja, (end - start) NÃO pode ser menor que {min_len}. Um corte de \
redes sociais precisa contar uma ideia completa — trechos de 8-10 segundos são \
CURTOS DEMAIS e serão rejeitados. Escolha começo e fim que englobem a ideia inteira \
(pergunta + resposta + fecho), não só uma frase solta.

Cada corte DEVE seguir o padrão viral:
- HOOK (0-3s): frase de impacto que prende a atenção imediatamente
- DESENVOLVIMENTO: história, argumento ou revelação que sustenta a retenção
- CTA: fechamento que gera engajamento (pergunta, loop, chamada)

Priorize: ganchos fortes, polêmica saudável, storytelling, números/dados, \
revelações, emoção. Comece o corte EXATAMENTE onde começa uma frase forte.

TÍTULO E DESCRIÇÃO PARA O YOUTUBE — siga o padrão dos canais de corte que \
funcionam. Estude estes exemplos reais e imite o ESTILO (não o assunto):

- "O SEGREDO INCRÍVEL DOS PLANADORES #aviação #avião #curiosidades"
- "Celso Portiolli conta história engraçada que passou com silvio santos #podcast"
- "LITO: EU JAMAIS SALTO DE PARAQUEDAS #curiosidades #avião"
- "Como a ONÇA preda o JACARÉ 🐆🐊 Richard Rasmussen REVELA"
- "Essa foca não está bebendo água e sim salvando o planeta de uma enchente"

Regras do `yt_title` (máx 100 caracteres):
- crie CURIOSIDADE ou contradição — o leitor precisa querer saber o resto
- se houver pessoa conhecida na fala, cite o NOME (dá autoridade e busca)
- destaque 2-4 palavras-chave em MAIÚSCULAS (não o título inteiro)
- formatos que funcionam: "NOME: frase de impacto" · "Como X faz Y — NOME REVELA" \
· "O que ninguém te contou sobre X" · afirmação surpreendente e específica
- termine com 2-4 hashtags do tema (ex.: #podcast #curiosidades)
- proibido: clickbait que a fala não entrega, "você não vai acreditar", \
"IMPERDÍVEL", frases genéricas

Regras do `yt_description` — CURTA, é uma chamada, NÃO um resumo do vídeo:
- NO MÁXIMO 3 linhas de texto + a linha de hashtags. Nada de parágrafo \
  explicando o conteúdo: quem vai assistir já vai ver o vídeo.
- linha 1: UMA pergunta curta ligada ao assunto do corte, que dê vontade de \
  responder nos comentários (use o tema real da fala, não algo genérico)
- linha 2: convite curto para se inscrever/seguir, com a ver com o tema
- linha 3: 4-6 hashtags do tema
Exemplo do tamanho e do tom esperados:
"E você, teria coragem de saltar? 👇\n\nSe inscreva para mais histórias de \
aviação 🛩️\n\n#aviação #paraquedas #curiosidades #podcast"
Escreva em português do Brasil, natural. Não invente fatos.

Transcrição:
{transcript}

Responda APENAS com JSON válido:
{{"clips": [{{"start": <seg>, "end": <seg>, "title": "<título viral curto>",
"headline": "<A FRASE MAIS IMPACTANTE do trecho, máx 7 palavras, com 1-2 \
emojis no final relacionados ao tema (VARIE os emojis entre os cortes, nunca \
repita), para a tarja fixa do vídeo — estilo manchete de corte viral>",
"yt_title": "<título para o YouTube seguindo as regras acima>",
"yt_description": "<descrição para o YouTube seguindo as regras acima>",
"hook": "<texto do hook>", "development": "<resumo do desenvolvimento>",
"cta": "<texto/descrição do cta>", "score": <0-100>,
"reason": "<por que viraliza>"}}]}}"""


def timestamped_transcript(words, every=8):
    """Texto com marcador [Ns] a cada `every` segundos para a IA localizar trechos."""
    out, last = [], -every
    for w in words:
        if w["s"] - last >= every:
            out.append(f"[{w['s']:.0f}s]")
            last = w["s"]
        out.append(w["w"])
    return " ".join(out)


def _parse_clips_json(raw):
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)["clips"]


# palavras sem valor como hashtag
_STOP = {"para", "como", "isso", "mais", "muito", "quando", "porque", "sobre",
         "então", "sempre", "todo", "toda", "cada", "onde", "qual", "quem",
         "esse", "essa", "este", "esta", "aquele", "pelo", "pela", "você",
         "vocês", "gente", "coisa", "coisas", "tudo", "nada", "aqui", "hoje",
         "agora", "depois", "antes", "ainda", "também", "assim", "fazer",
         "sendo", "estar", "tinha", "havia", "seria", "pode", "vai", "foi"}


def _hashtags(texto, n=5):
    """Tira do próprio conteúdo as palavras que viram hashtag."""
    palavras = re.findall(r"[A-Za-zÀ-ÿ]{4,}", (texto or "").lower())
    vistos, tags = set(), []
    for p in palavras:
        if p in _STOP or p in vistos:
            continue
        vistos.add(p)
        tags.append("#" + p)
        if len(tags) >= n:
            break
    return tags


def montar_publicacao(clip, job):
    """Garante título e descrição prontos para publicar.

    A IA já devolve `yt_title`/`yt_description`; isto cobre os casos em que ela
    não devolveu (motor heurístico, projetos antigos, resposta incompleta).
    Antes disso a descrição vinha do campo `reason`, que é a análise interna de
    'por que viraliza' — texto errado para mostrar ao público."""
    titulo = (clip.get("yt_title") or "").strip()
    desc = (clip.get("yt_description") or "").strip()
    base = (clip.get("headline") or clip.get("title") or "").strip()
    base = EMOJI_RE.sub("", base).strip(" -–—:")
    fonte = (job or {}).get("video_title") or ""
    assunto = " ".join(x for x in (base, clip.get("hook", ""),
                                   clip.get("development", "")) if x)

    if not titulo:
        titulo = base or (clip.get("hook") or "Corte")[:70]
        tags = _hashtags(assunto, 3)
        if tags:
            titulo = f"{titulo} {' '.join(tags)}"
        titulo = titulo[:100]

    if not desc:
        # curta de propósito: é chamada para comentário, não resumo do vídeo
        pergunta = (clip.get("cta") or "").strip()
        if not pergunta or len(pergunta) > 120:
            pergunta = "E você, o que acha disso? Comenta aí 👇"
        if not pergunta.endswith(("👇", "?", "!")):
            pergunta += " 👇"
        desc = (f"{pergunta}\n\n"
                "🔔 Se inscreva para mais cortes como este\n\n"
                + " ".join(_hashtags(assunto, 5)))
    return titulo[:100], desc


def _build_prompt(job, transcript):
    return ANALYSIS_PROMPT.format(
        n=job["num_clips"], min_len=job["min_len"], max_len=job["max_len"],
        transcript=timestamped_transcript(transcript["words"])[:150_000])


def analyze_with_claude(job, transcript):
    import anthropic
    client = anthropic.Anthropic(api_key=job["api_key"])
    msg = client.messages.create(
        model="claude-sonnet-5", max_tokens=4000,
        messages=[{"role": "user", "content": _build_prompt(job, transcript)}])
    return _parse_clips_json(msg.content[0].text)


def eh_chave_openai(k):
    """Chaves da OpenAI: sk-… e sk-proj-… (mas NÃO sk-ant-, que é da Anthropic)."""
    k = (k or "").strip()
    return k.startswith("sk-") and not k.startswith("sk-ant")


def erro_openai(r):
    """Extrai a mensagem real do erro devolvido pela OpenAI."""
    try:
        e = (r.json() or {}).get("error") or {}
        msg = e.get("message") or ""
        cod = e.get("code") or e.get("type") or ""
        return f"{r.status_code} {cod}: {msg}"[:400]
    except Exception:
        return f"{r.status_code}: {(r.text or '')[:300]}"


def _openai_call(api_key, model, prompt, max_tokens=8000):
    """Chama a API da OpenAI (chat/completions).

    Os modelos novos (gpt-5.x) são de raciocínio e sofrem do mesmo mal que o
    Gemini: gastam o orçamento de saída "pensando" e devolvem conteúdo vazio.
    Por isso mandamos `reasoning_effort: none`.

    A API também troca de parâmetros conforme o modelo (`max_tokens` virou
    `max_completion_tokens`), então, em vez de chutar, mandamos o formato novo
    e removemos automaticamente o que aquele modelo específico recusar."""
    import requests
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    corpo = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "reasoning_effort": "none",     # não gastar saída pensando
    }
    for _ in range(4):
        r = requests.post(url, headers=headers, json=corpo, timeout=300)
        if r.status_code != 400:
            return r
        # 400 costuma ser "parâmetro não suportado por este modelo":
        # tira o que ele reclamou e tenta de novo, em vez de desistir.
        try:
            msg = ((r.json().get("error") or {}).get("message") or "").lower()
        except Exception:
            return r
        if "reasoning_effort" in msg and "reasoning_effort" in corpo:
            corpo.pop("reasoning_effort")
        elif ("max_completion_tokens" in msg
              and "max_completion_tokens" in corpo):
            corpo["max_tokens"] = corpo.pop("max_completion_tokens")
        elif "max_tokens" in msg and "max_tokens" in corpo:
            corpo["max_completion_tokens"] = corpo.pop("max_tokens")
        elif "response_format" in msg and "response_format" in corpo:
            corpo.pop("response_format")
        elif "temperature" in msg and "temperature" in corpo:
            corpo.pop("temperature")
        else:
            return r                     # 400 de verdade: devolve o erro real
    return r


def texto_openai(j):
    """Extrai o texto da resposta. Retorna (texto, motivo_do_problema)."""
    escolhas = j.get("choices") or []
    if not escolhas:
        return None, "a OpenAI não devolveu nenhuma resposta"
    c0 = escolhas[0]
    texto = ((c0.get("message") or {}).get("content") or "").strip()
    if texto:
        return texto, None
    motivo = c0.get("finish_reason") or "?"
    if motivo == "length":
        return None, ("o limite de tokens acabou antes da resposta "
                      "(o modelo gastou tudo raciocinando)")
    if motivo == "content_filter":
        return None, "a OpenAI bloqueou a resposta por filtro de conteúdo"
    return None, f"resposta vazia da OpenAI (finish_reason={motivo})"


def analyze_with_openai(job, transcript):
    """Análise viral com ChatGPT (OpenAI), com as mesmas proteções do Gemini."""
    import time
    prompt = _build_prompt(job, transcript)
    escolhido = (job.get("openai_model") or CONFIG.get("openai_model") or
                 os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")).strip()
    modelos = [escolhido] + [m for m in ("gpt-5.6-terra", "gpt-5.6-luna")
                             if m != escolhido]
    ultimo = None
    for model in modelos:
        for tentativa in range(3):
            r = _openai_call(job["api_key"], model, prompt)

            if r.status_code == 429:
                ultimo = erro_openai(r)
                if erro_permanente_429(r):
                    raise RuntimeError(ultimo)      # saldo: esperar não resolve
                if tentativa < 2:
                    espera = int(r.headers.get("retry-after", 0) or 0) \
                        or (5 * 2 ** tentativa)
                    set_status(job, "analisando", 65,
                               f"Limite de ritmo da OpenAI — aguardando "
                               f"{min(espera, 60)}s (tentativa {tentativa + 2}/3)")
                    time.sleep(min(espera, 60))
                    continue
                break

            if r.status_code != 200:
                ultimo = erro_openai(r)
                if r.status_code in (401, 403):
                    raise RuntimeError(ultimo)      # chave ruim: não insista
                break

            texto, problema = texto_openai(r.json())
            if problema:
                ultimo = problema
                if "limite de tokens" in problema and tentativa < 2:
                    r = _openai_call(job["api_key"], model, prompt,
                                     max_tokens=16000)
                    if r.status_code == 200:
                        texto, problema = texto_openai(r.json())
                if problema:
                    break
            job["engine"] = f"openai:{model}"
            return _parse_clips_json(texto)
    raise RuntimeError(ultimo or "OpenAI não respondeu.")


def eh_chave_gemini(k):
    """Aceita os dois formatos do Google: AIza… (antigo) e AQ.… (novo)."""
    k = (k or "").strip()
    return k.startswith("AIza") or k.startswith("AQ.")


def _gemini_call(api_key, model, prompt, max_tokens=8192):
    """Chama o Gemini.

    Dois detalhes que quebravam a integração e já custaram caro:

    1) Os modelos 2.5 "pensam" antes de responder e os tokens desse raciocínio
       saem do MESMO orçamento de `maxOutputTokens`. Com o pensamento ligado,
       o modelo gastava a cota toda pensando e devolvia 200 com a resposta
       VAZIA (finishReason=MAX_TOKENS). Por isso `thinkingBudget: 0`.
    2) `Authorization: Bearer` só vale para token OAuth, não para chave de API.
       Usar isso como fallback em erro 400 fazia o erro real do Google ser
       substituído por um "credencial inválida" enganoso.
    """
    import requests
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    corpo = {"contents": [{"parts": [{"text": prompt}]}],
             "generationConfig": {"temperature": 0.4,
                                  "maxOutputTokens": max_tokens,
                                  "responseMimeType": "application/json",
                                  # sem isto o 2.5 responde vazio
                                  "thinkingConfig": {"thinkingBudget": 0}}}
    h = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    r = requests.post(url, headers=h, json=corpo, timeout=300)
    if r.status_code in (401, 403):
        # só aqui faz sentido tentar OAuth; se falhar, devolve o erro original
        alt = requests.post(url, headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": "application/json"},
                            json=corpo, timeout=300)
        if alt.status_code == 200:
            return alt
    if r.status_code == 400 and "thinkingConfig" in json.dumps(corpo):
        # modelos antigos (1.5/2.0) não conhecem thinkingConfig — repete sem ele
        corpo["generationConfig"].pop("thinkingConfig", None)
        r2 = requests.post(url, headers=h, json=corpo, timeout=300)
        if r2.status_code == 200:
            return r2
    return r


def erro_google(r):
    """Extrai a mensagem real do erro devolvido pela API."""
    try:
        j = r.json()
        e = j.get("error", {})
        return f"{r.status_code} {e.get('status', '')}: {e.get('message', '')}"[:400]
    except Exception:
        return f"{r.status_code}: {(r.text or '')[:300]}"


def texto_da_resposta(j):
    """Extrai o texto da resposta do Gemini.

    Retorna (texto, motivo_do_problema). Uma resposta 200 pode vir sem texto:
    bloqueio de segurança, ou orçamento de tokens estourado."""
    cands = j.get("candidates") or []
    if not cands:
        fb = (j.get("promptFeedback") or {}).get("blockReason")
        if fb:
            return None, f"o Gemini bloqueou o pedido ({fb})"
        return None, "o Gemini não devolveu nenhuma resposta"
    c0 = cands[0]
    partes = ((c0.get("content") or {}).get("parts")) or []
    texto = "".join(p.get("text", "") for p in partes).strip()
    if texto:
        return texto, None
    motivo = c0.get("finishReason") or "?"
    if motivo == "MAX_TOKENS":
        return None, ("o limite de tokens acabou antes da resposta "
                      "(maxOutputTokens curto demais)")
    if motivo == "SAFETY":
        return None, "o Gemini bloqueou a resposta por filtro de segurança"
    return None, f"resposta vazia do Gemini (finishReason={motivo})"


# Um 429 pode ser duas coisas MUITO diferentes: "você está mandando rápido
# demais" (esperar resolve) ou "acabou o saldo" (esperar nunca resolve).
# Confundir os dois faz o usuário esperar à toa achando que é problema nosso.
SINAIS_SEM_SALDO = (
    "credit_balance_exhausted", "no credits remaining", "insufficient_quota",
    "insufficient_quota", "exceeded your current quota", "prepayment",
    "credits are depleted", "billing", "check your plan", "add credits",
    "quota exceeded for quota metric", "free tier", "billing_not_active",
    "account is not active", "spending limit",
)


def sem_saldo(texto):
    """True quando a mensagem de erro indica falta de saldo/plano, não ritmo."""
    t = (texto or "").lower()
    return any(s in t for s in SINAIS_SEM_SALDO)


def erro_permanente_429(r):
    """Distingue 'excedeu o ritmo' de problema de saldo/plano."""
    try:
        j = r.json() or {}
        e = j.get("error") or {}
        # olha mensagem, código e tipo — provedores usam campos diferentes
        alvo = " ".join(str(e.get(c, "")) for c in
                        ("message", "code", "type", "status"))
    except Exception:
        alvo = (r.text or "")[:500]
    return sem_saldo(alvo)


def analyze_with_gemini(job, transcript):
    """Análise viral com Google Gemini. Retenta apenas o que vale a pena
    retentar e sempre devolve o motivo real da falha."""
    import time
    prompt = _build_prompt(job, transcript)
    escolhido = (job.get("gemini_model") or
                 os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")).strip()
    modelos = [escolhido] + [m for m in ("gemini-2.5-flash",
                                         "gemini-2.5-flash-lite")
                             if m != escolhido]
    ultimo = None
    for model in modelos:
        for tentativa in range(3):
            r = _gemini_call(job["api_key"], model, prompt)

            if r.status_code == 429:
                ultimo = erro_google(r)
                if erro_permanente_429(r):
                    # saldo/plano: nem este modelo nem os próximos vão passar
                    raise RuntimeError(ultimo)
                if tentativa < 2:
                    espera = int(r.headers.get("Retry-After", 0) or 0) \
                        or (5 * 2 ** tentativa)
                    set_status(job, "analisando", 65,
                               f"Limite de ritmo do Gemini — aguardando "
                               f"{min(espera, 60)}s (tentativa {tentativa + 2}/3)")
                    time.sleep(min(espera, 60))
                    continue
                break                                  # tenta o próximo modelo

            if r.status_code != 200:
                ultimo = erro_google(r)
                # chave inválida/sem permissão não melhora trocando de modelo
                if r.status_code in (401, 403):
                    raise RuntimeError(ultimo)
                break

            texto, problema = texto_da_resposta(r.json())
            if problema:
                ultimo = problema
                if "limite de tokens" in problema and tentativa < 2:
                    # dá mais espaço e tenta de novo no mesmo modelo
                    r = _gemini_call(job["api_key"], model, prompt,
                                     max_tokens=16384)
                    if r.status_code == 200:
                        texto, problema = texto_da_resposta(r.json())
                if problema:
                    break
            job["engine"] = f"gemini:{model}"
            return _parse_clips_json(texto)
    raise RuntimeError(ultimo or "Gemini não respondeu.")


def analyze_with_ai(job, transcript):
    """Escolhe o provedor: opção explícita na tela ou, no automático,
    detecção pelo formato da chave.
    AIza…/AQ.… = Gemini | sk-ant-… = Claude | sk-…/sk-proj-… = ChatGPT."""
    key = chave_efetiva(job)
    job["api_key"] = key
    choice = job.get("engine_choice", "auto")

    # escolha explícita do usuário manda, mas avisa se a chave não combina
    if choice == "gemini":
        if not eh_chave_gemini(key):
            raise RuntimeError("Você escolheu Gemini, mas a chave salva não é "
                               "do Google (deve começar com AIza ou AQ.).")
        return analyze_with_gemini(job, transcript)
    if choice == "openai":
        if not eh_chave_openai(key):
            raise RuntimeError("Você escolheu ChatGPT, mas a chave salva não é "
                               "da OpenAI (deve começar com sk-).")
        return analyze_with_openai(job, transcript)

    # automático: descobre pelo formato da chave
    if eh_chave_gemini(key):
        return analyze_with_gemini(job, transcript)
    if key.startswith("sk-ant"):
        job["engine"] = "claude"
        return analyze_with_claude(job, transcript)
    if eh_chave_openai(key):
        return analyze_with_openai(job, transcript)
    raise RuntimeError("Formato de chave não reconhecido. Use uma chave do "
                       "Gemini (AIza…/AQ.…) ou da OpenAI (sk-…).")


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def analyze_with_ollama(job, transcript):
    """Análise viral com modelo local via Ollama (grátis, sem chave)."""
    import requests
    model = (job.get("ollama_model") or
             os.environ.get("OLLAMA_MODEL", "llama3.1")).strip()
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={"model": model, "stream": False,
              "format": "json",                      # força saída JSON
              "keep_alive": 0,                       # libera a RAM logo após analisar
              "options": {"temperature": 0.4, "num_ctx": 8192},
              "messages": [{"role": "user",
                            "content": _build_prompt(job, transcript)}]},
        timeout=600)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    job["engine"] = f"ollama:{model}"
    # garante o descarregamento do modelo da memória antes de renderizar
    try:
        requests.post(f"{OLLAMA_URL}/api/generate",
                      json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass
    return _parse_clips_json(content)


TRIGGERS = ["segredo", "ninguém", "nunca", "sempre", "erro", "verdade", "dinheiro",
            "grátis", "como", "por que", "porque", "cuidado", "atenção", "incrível",
            "melhor", "pior", "proibido", "milhão", "mil", "todo mundo", "você",
            "descobri", "mudou", "chocante", "simples", "rápido", "fácil", "problema"]


def analyze_heuristic(job, transcript):
    """Fallback sem API: pontua janelas por gatilhos virais, perguntas e números."""
    words = transcript["words"]
    if not words:
        return []
    total = words[-1]["e"]
    target = (job["min_len"] + job["max_len"]) / 2
    # início de frase = palavra após pausa > 0.6s ou pontuação final
    starts = [0]
    for i in range(1, len(words)):
        if words[i]["s"] - words[i - 1]["e"] > 0.6 or \
           words[i - 1]["w"].endswith((".", "!", "?")):
            starts.append(i)
    cands = []
    for si in starts:
        s = words[si]["s"]
        e = min(s + target, total)
        if e - s < job["min_len"]:
            continue
        chunk = [w for w in words if s <= w["s"] <= e]
        text = " ".join(w["w"] for w in chunk).lower()
        score = sum(2 for t in TRIGGERS if t in text)
        score += 3 * text[:120].count("?")           # pergunta no hook
        score += len(re.findall(r"\d", text[:200])) * 0.5
        cands.append({"start": round(s, 2), "end": round(e, 2), "score": score,
                      "text": " ".join(w["w"] for w in chunk)})
    cands.sort(key=lambda c: -c["score"])
    picked = []
    for c in cands:                                   # sem sobreposição
        if all(c["end"] < p["start"] or c["start"] > p["end"] for p in picked):
            picked.append(c)
        if len(picked) >= job["num_clips"]:
            break
    mx = max((c["score"] for c in picked), default=1) or 1
    def _headline(text):
        """Primeira frase (ou trecho até ~42 chars), cortada em palavra."""
        first = re.split(r"[.!?]", text)[0]
        if len(first) <= 42:
            return first.strip()
        cut = first[:42].rsplit(" ", 1)[0]
        return cut.strip() + "…"

    return [{"start": c["start"], "end": c["end"],
             "headline": _headline(c["text"]) + " " + pick_emoji(c["text"], i),
             "title": c["text"][:60] + "…", "hook": c["text"][:100],
             "development": "Trecho selecionado por heurística local.",
             "cta": "Adicione um CTA na edição final.",
             "score": round(100 * c["score"] / mx),
             "reason": "Gatilhos virais detectados na fala."}
            for i, c in enumerate(picked)]


# --------------------------------------------------------------------------
# 4. Legendas (ASS karaokê palavra-por-palavra + SRT)
# --------------------------------------------------------------------------

# Modelos de legenda: cada um define fonte, cor, contorno, sombra e posição.
# Cores no formato ASS &HAABBGGRR (AA=00 opaco, FF transparente).
TEMPLATES = {
    "viral-bold": {
        "nome": "Viral Bold (contorno grosso, palavra em amarelo)",
        "font": "Arial Black", "size": 88, "margin_v": 420,
        "primary": "&H0000E7FF",     # cor da palavra falada (amarelo)
        "secondary": "&H00FFFFFF",   # demais palavras (branco)
        "outline_col": "&H00000000", "back": "&H96000000",
        "outline": None, "shadow": 2, "bold": -1,
        "caps": True, "karaoke": True, "caixa": "upper",
    },
    "sora-clean": {
        "nome": "Sora Clean (branco, sombra suave, sem caixa alta)",
        "font": "Sora", "size": 58, "margin_v": 330,
        "primary": "&H00FFFFFF", "secondary": "&H00FFFFFF",
        "outline_col": "&H64000000", "back": "&HB4000000",
        "outline": 1, "shadow": 3, "bold": -1,
        "caps": False, "karaoke": False, "caixa": "sentenca",
    },
    "sora-destaque": {
        "nome": "Sora Destaque (limpo, com palavra falada em amarelo)",
        "font": "Sora", "size": 58, "margin_v": 330,
        "primary": "&H0000E7FF", "secondary": "&H00FFFFFF",
        "outline_col": "&H64000000", "back": "&HB4000000",
        "outline": 1, "shadow": 3, "bold": -1,
        "caps": False, "karaoke": True, "caixa": "sentenca",
    },
}


def tpl(nome):
    return TEMPLATES.get(nome or "viral-bold", TEMPLATES["viral-bold"])


FIM_FRASE = ".!?…:"


def aplicar_caixa(blocos_tokens, modo):
    """Ajusta a caixa do texto conforme o modelo.
    'upper'    = tudo em CAIXA ALTA
    'sentenca' = mantém o texto como está, apenas garante maiúscula no
                 começo e depois de ponto/interrogação/exclamação
    'manter'   = não mexe"""
    if modo == "upper":
        return [[w.upper() for w in toks] for toks in blocos_tokens]
    if modo != "sentenca":
        return blocos_tokens
    saida, novo = [], True
    for toks in blocos_tokens:
        linha = []
        for w in toks:
            if novo:
                for i, ch in enumerate(w):
                    if ch.isalpha():
                        w = w[:i] + ch.upper() + w[i + 1:]
                        break
            linha.append(w)
            limpo = w.rstrip('"\'”’)]')
            if limpo and limpo[-1] in FIM_FRASE:
                novo = True
            elif w.strip():
                novo = False
        saida.append(linha)
    return saida


def ass_header(font=None, size=88, margin_v=420, template=None):
    t = tpl(template)
    f = font or t["font"] or font_family()
    s = max(24, min(int(size or t["size"]), 200))
    contorno = t["outline"] if t["outline"] is not None else max(3, round(s * 0.08))
    mv = int(margin_v if margin_v is not None else t["margin_v"])
    return (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Viral,{f},{s},{t['primary']},{t['secondary']},"
        f"{t['outline_col']},{t['back']},{t['bold']},0,0,0,100,100,0,0,1,"
        f"{contorno},{t['shadow']},2,60,60,{mv},1\n"
        f"Style: Headline,{f},68,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,"
        "-1,0,0,0,100,100,0,0,1,6,3,8,70,70,150,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n")


@app.get("/api/templates")
def list_templates():
    return {"templates": [{"id": k, "nome": v["nome"], "font": v["font"],
                           "size": v["size"], "caps": v["caps"],
                           "karaoke": v["karaoke"], "margin_v": v["margin_v"]}
                          for k, v in TEMPLATES.items()]}


def ass_time(t):
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def srt_time(t):
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h:02d}:{m:02d}:{int(t % 60):02d},{int(t * 1000 % 1000):03d}"


PAUSA_MAX = 0.45      # silêncio (s) que já quebra a linha da legenda
TERMINA_FRASE = re.compile(r"[.!?…:;]$")


def group_lines(words, clip_start, clip_end, per_line=3, groups=None):
    """Agrupa as palavras do trecho em linhas de legenda.

    Agrupar de N em N cegamente deixa a legenda presa na tela durante os
    silêncios: se o locutor pausa 5s no meio de um grupo, a linha inteira fica
    aparecendo nesse tempo todo — parecendo legenda antiga fora de sincronia.
    Por isso a linha também quebra quando há pausa longa ou fim de frase.

    `groups`: quantidade de palavras por linha definida pelo editor (o usuário
    mandou, então respeita)."""
    cw = [w for w in words if clip_start <= w["s"] < clip_end and w["w"]]
    if groups:
        out, i = [], 0
        for n in groups:
            n = max(1, int(n))
            if i >= len(cw):
                break
            out.append(cw[i:i + n])
            i += n
        while i < len(cw):                  # sobras -> linhas padrão
            out.append(cw[i:i + per_line])
            i += per_line
        return out

    out, atual = [], []
    for w in cw:
        if atual:
            pausa = w["s"] - atual[-1]["e"]
            if (len(atual) >= per_line or pausa > PAUSA_MAX
                    or TERMINA_FRASE.search(atual[-1]["w"])):
                out.append(atual)
                atual = []
        atual.append(w)
    if atual:
        out.append(atual)
    return out


def build_subs(words, clip_start, clip_end, ass_path, srt_path, per_line=3,
               headline=None, groups=None, font=None, size=None,
               margin_v=None, template=None):
    """Linhas de até `per_line` palavras com preenchimento karaokê (\\k).
    `headline`: frase de impacto fixa no topo durante o corte inteiro."""
    t = tpl(template)
    font = font or t["font"]
    size = size or t["size"]
    lines = group_lines(words, clip_start, clip_end, per_line, groups)
    caixa = aplicar_caixa([[w["w"] for w in ln] for ln in lines],
                          t.get("caixa", "upper"))
    ass, srt = [], []
    for n, line in enumerate(lines, 1):
        s = line[0]["s"] - clip_start
        e = min(line[-1]["e"], clip_end) - clip_start
        # a linha some logo após a última palavra (com um respiro para leitura)
        # e nunca invade a próxima — assim não sobra legenda velha no silêncio
        e += 0.35
        if n < len(lines):                       # lines[n] = próxima linha
            e = min(e, lines[n][0]["s"] - clip_start - 0.05)
        e = min(e, clip_end - clip_start)
        e = max(e, s + 0.2)
        parts = []
        for wi, w in enumerate(line):
            palavra = caixa[n - 1][wi]
            if t["karaoke"]:
                dur_cs = max(1, int((w["e"] - w["s"]) * 100))
                parts.append(f"{{\\k{dur_cs}}}{palavra}")
            else:
                parts.append(palavra)
        ass.append(f"Dialogue: 0,{ass_time(max(0, s))},{ass_time(e)},Viral,,0,0,0,,"
                   + " ".join(parts))
        srt.append(f"{n}\n{srt_time(max(0, s))} --> {srt_time(e)}\n"
                   + " ".join(caixa[n - 1]) + "\n")
    if headline:
        txt = re.sub(r"[{}\\]", "", headline).strip().upper()
        dur = clip_end - clip_start
        ass.insert(0, f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Headline,,"
                      f"0,0,0,,{txt}")
    ass_path.write_text(
        ass_header(font, size, margin_v, template) + "\n".join(ass),
        encoding="utf-8")
    srt_path.write_text("\n".join(srt), encoding="utf-8")


def build_subs_blocks(blocks, ass_path, srt_path, font=None, size=88,
                      headline=None, dur=0, margin_v=None, template=None):
    """Gera legenda a partir dos blocos do editor (tempos JÁ na linha do tempo
    de saída). Usa a mesma distribuição por nº de caracteres do preview, então
    o vídeo final fica idêntico ao que aparece na tela."""
    t = tpl(template)
    ordenados = sorted(blocks, key=lambda x: x["s"])
    caixa = aplicar_caixa([(b.get("text") or "").split() for b in ordenados],
                          t.get("caixa", "upper"))
    ass, srt, n = [], [], 0
    for bi, b in enumerate(ordenados):
        toks = caixa[bi]
        if not toks:
            continue
        s = max(0.0, float(b["s"]))
        e = max(s + 0.15, float(b["e"]))
        tot = sum(len(w) for w in toks) or 1
        parts = []
        for w in toks:
            palavra = w
            if t["karaoke"]:
                d = (e - s) * len(w) / tot
                parts.append(f"{{\\k{max(1, int(d * 100))}}}{palavra}")
            else:
                parts.append(palavra)
        n += 1
        ass.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Viral,,0,0,0,,"
                   + " ".join(parts))
        srt.append(f"{n}\n{srt_time(s)} --> {srt_time(e)}\n"
                   + " ".join(toks) + "\n")
    if headline:
        txt = re.sub(r"[{}\\]", "", headline).strip().upper()
        ass.insert(0, f"Dialogue: 1,{ass_time(0)},{ass_time(max(dur, 1))},"
                      f"Headline,,0,0,0,,{txt}")
    Path(ass_path).write_text(
        ass_header(font, size, margin_v, template) + "\n".join(ass),
        encoding="utf-8")
    Path(srt_path).write_text("\n".join(srt), encoding="utf-8")


# --------------------------------------------------------------------------
# 4b. Tarja com a frase de efeito (estilo cortes virais)
# --------------------------------------------------------------------------

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0001F000-\U0001F0FF❤️]+")

EMOJI_MAP = [
    (r"dinheiro|milh[aã]o|mil reais|rico|renda|lucro|investi", "💰"),
    (r"segredo|ningu[ée]m te conta|escondid", "🤫"),
    (r"erro|errad|cuidado|aten[çc][ãa]o|perig", "⚠️"),
    (r"chocante|inacredit|absurd|surre|loucura", "😱"),
    (r"verdade|prova|fato|revela", "👀"),
    (r"nunca|proibid|pare de", "🚫"),
    (r"r[áa]pido|agora|urgente|corre", "⚡"),
]
FALLBACK_EMOJIS = ["🔥", "😱", "👀", "💣", "⚡", "🚨", "🤯", "💥"]


def pick_emoji(text, i=0):
    """Escolhe emoji pelo conteúdo da fala; rotaciona quando não há match."""
    low = (text or "").lower()
    for pat, e in EMOJI_MAP:
        if re.search(pat, low):
            return e
    return FALLBACK_EMOJIS[i % len(FALLBACK_EMOJIS)]


FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",     # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Linux (teste)
]


def _load_font(size):
    from PIL import ImageFont
    if CUSTOM_FONT["path"] and os.path.exists(CUSTOM_FONT["path"]):
        try:
            return ImageFont.truetype(CUSTOM_FONT["path"], size)
        except Exception:
            pass
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _emoji_img(chars, target_h):
    """Renderiza emojis coloridos (Apple Color Emoji) e redimensiona."""
    from PIL import Image, ImageDraw, ImageFont
    paths = ["/System/Library/Fonts/Apple Color Emoji.ttc",
             "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"]
    for p in paths:
        if not os.path.exists(p):
            continue
        for s in (160, 137, 128, 109, 96, 64):
            try:
                f = ImageFont.truetype(p, s)
                im = Image.new("RGBA", (s * (len(chars) + 1), int(s * 1.5)),
                               (0, 0, 0, 0))
                ImageDraw.Draw(im).text((0, 0), chars, font=f,
                                        embedded_color=True)
                box = im.getbbox()
                if not box:
                    continue
                im = im.crop(box)
                r = target_h / im.height
                return im.resize((max(1, int(im.width * r)), target_h))
            except Exception:
                continue
    return None


def make_banner(headline, out_png, width=1080):
    """Tarja vermelha centralizada: 1ª linha branca, 2ª amarela, emojis no fim."""
    from PIL import Image, ImageDraw
    text = EMOJI_RE.sub("", headline).strip().upper() or "ASSISTA ATÉ O FINAL"
    emojis = "".join(EMOJI_RE.findall(headline))[:3]
    words, lines, cur = text.split(), [], ""
    for w in words:
        if not cur or len(cur) + 1 + len(w) <= 24:
            cur = (cur + " " + w).strip()
        else:
            lines.append(cur)
            cur = w
            if len(lines) == 2:
                break
    if cur and len(lines) < 2:
        lines.append(cur)
    lines = lines[:2]

    fs = 58
    font = _load_font(fs)
    lh = int(fs * 1.35)
    pad_y = 30
    h = pad_y * 2 + lh * len(lines)
    img = Image.new("RGBA", (width, h + 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([12, 14, width - 1, h + 14], fill=(0, 0, 0, 255))     # sombra
    d.rectangle([0, 0, width - 14, h], fill=(211, 18, 25, 255))       # tarja
    colors = [(255, 255, 255, 255), (255, 230, 0, 255)]
    if len(lines) == 1:
        colors = [(255, 255, 255, 255)]
    eimg = _emoji_img(emojis, fs) if emojis else None
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=font)
        extra = (eimg.width + 14) if (eimg and i == len(lines) - 1) else 0
        x = max(20, (width - 14 - tw - extra) / 2)
        y = pad_y + i * lh
        d.text((x, y), ln, font=font, fill=colors[min(i, len(colors) - 1)],
               stroke_width=5, stroke_fill=(0, 0, 0, 255))
        if eimg and i == len(lines) - 1:
            img.paste(eimg, (int(x + tw + 14), int(y + 4)), eimg)
    img.save(out_png)


def grab_still(video, t, out_png, cor=None):
    """Extrai um frame do vídeo para a parte fixa de cima (com a mesma
    correção de cor do vídeo, para não destoar)."""
    cmd = [FFMPEG, "-y", "-ss", str(max(0, t)), "-i",
           str(Path(video).resolve())]
    cf, _ = color_filter(video, cor)
    if cf:
        cmd += ["-vf", cf]
    cmd += ["-frames:v", "1", str(out_png)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        subprocess.run([FFMPEG, "-y", "-ss", str(max(0, t)), "-i",
                        str(Path(video).resolve()), "-frames:v", "1",
                        str(out_png)], check=True, capture_output=True,
                       text=True)


# --------------------------------------------------------------------------
# 5. Corte (ffmpeg — 9:16 1080x1920, crop central, legenda queimada)
# --------------------------------------------------------------------------

_CAPS = {}


def tem_tonemap():
    """Nem todo ffmpeg traz zscale/tonemap; sem eles o filtro HDR quebra."""
    if "tonemap" not in _CAPS:
        try:
            fl = subprocess.run([FFMPEG, "-filters"], capture_output=True,
                                text=True).stdout
            _CAPS["tonemap"] = ("zscale" in fl and "tonemap" in fl)
        except Exception:
            _CAPS["tonemap"] = False
    return _CAPS["tonemap"]


def is_hdr(video):
    """Detecta vídeo HDR (iPhone grava em HLG/Dolby Vision por padrão)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer,color_primaries",
             "-of", "csv=p=0", str(Path(video).resolve())],
            capture_output=True, text=True)
        s = (r.stdout or "").lower()
        return any(k in s for k in ("arib-std-b67", "smpte2084", "bt2020"))
    except Exception:
        return False


def color_filter(video, cor):
    """Cadeia de correção de cor. `cor` = dict com auto_hdr, brilho,
    contraste, saturacao, temperatura (0 = desligado)."""
    if not cor:
        return "", False
    partes, usou_tonemap = [], False
    if cor.get("auto_hdr", True) and tem_tonemap() and is_hdr(video):
        # HDR (HLG/PQ) -> SDR bt709, evita imagem lavada ou estourada
        partes.append("zscale=t=linear:npl=100,format=gbrpf32le,"
                      "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                      "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        usou_tonemap = True
    b = float(cor.get("brilho", 0) or 0)
    c = float(cor.get("contraste", 1) or 1)
    s = float(cor.get("saturacao", 1) or 1)
    if abs(b) > 0.001 or abs(c - 1) > 0.001 or abs(s - 1) > 0.001:
        partes.append(f"eq=brightness={b:.3f}:contrast={c:.3f}:"
                      f"saturation={s:.3f}")
    t = int(cor.get("temperatura", 0) or 0)
    if t:
        partes.append(f"colortemperature=temperature={t}")
    return (",".join(partes), usou_tonemap)


def video_duration(video):
    """Duração real do arquivo de vídeo (limite para esticar o corte)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(Path(video).resolve())],
            capture_output=True, text=True)
        return round(float(r.stdout.strip()), 2)
    except Exception:
        return 0.0


def has_audio(video):
    """Verifica se o vídeo tem faixa de áudio."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", str(Path(video).resolve())],
            capture_output=True, text=True)
        return bool(r.stdout.strip())
    except Exception:
        return True


def music_inputs(path=None):
    """Argumentos de entrada do ffmpeg para a música (com repetição).
    `path` permite trilha própria por corte (usado no lote)."""
    p = path or BG_MUSIC["path"]
    if not p or not os.path.exists(p):
        return []
    return ["-stream_loop", "-1", "-i", str(Path(p).resolve())]


def audio_graph(midx, mvol=12, mstart=0, vvol=100, tem_voz=True,
                tem_musica=True, fonte_audio=None):
    """Monta o trecho de áudio do filter_complex combinando a voz
    (`fonte_audio`, padrão [0:a]) com a música (faixa `midx`).
    Retorna (grafo|None, rótulo). Grafo None = usar o áudio direto."""
    voz = fonte_audio or "[0:a]"
    v = max(0, min(int(vvol), 200)) / 100.0
    m = max(0, min(int(mvol), 100)) / 100.0
    ms = max(0, int(float(mstart) * 1000))
    atraso = f",adelay={ms}:all=1" if ms else ""
    if not tem_musica:
        if not tem_voz:
            return None, "0:a?"
        if abs(v - 1.0) < 0.001 and fonte_audio is None:
            return None, "0:a?"                      # nada a alterar
        return f"{voz}volume={v:.3f}[aout]", "[aout]"
    if not tem_voz:
        return f"[{midx}:a]volume={m:.3f}{atraso}[aout]", "[aout]"
    return (f"{voz}volume={v:.3f}[voz];"
            f"[{midx}:a]volume={m:.3f}{atraso}[bgm];"
            f"[voz][bgm]amix=inputs=2:duration=first:normalize=0[aout]"), "[aout]"


def segments_prefix(segs, tem_audio):
    """Corta o vídeo em vários pedaços e emenda (permite tirar trechos do meio).
    Retorna (grafo, rótulo_vídeo, rótulo_áudio|None)."""
    p, vl, al = [], [], []
    for i, sg in enumerate(segs):
        s, e = float(sg["s"]), float(sg["e"])
        p.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[sv{i}]")
        vl.append(f"[sv{i}]")
        if tem_audio:
            p.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},"
                     f"asetpts=PTS-STARTPTS[sa{i}]")
            al.append(f"[sa{i}]")
    n = len(segs)
    if n > 1:
        p.append("".join(vl) + f"concat=n={n}:v=1:a=0[vsrc]")
        if tem_audio:
            p.append("".join(al) + f"concat=n={n}:v=0:a=1[asrc]")
    else:
        p.append(f"{vl[0]}null[vsrc]")
        if tem_audio:
            p.append(f"{al[0]}anull[asrc]")
    return ";".join(p), "[vsrc]", ("[asrc]" if tem_audio else None)


def overlay_graph(entrada, overlays, idx0):
    """Aplica imagens sobrepostas com tempo de entrada/saída."""
    if not overlays:
        return "", entrada, []
    partes, ins, atual = [], [], entrada
    for i, ov in enumerate(overlays):
        k = idx0 + i
        ins.append(str(Path(ov["path"]).resolve()))
        larg = max(40, int(float(ov.get("w", 0.5)) * 1080))
        x = int(float(ov.get("x", 0.5)) * 1080 - larg / 2)
        y = int(float(ov.get("y", 0.3)) * 1920)
        s, e = float(ov.get("s", 0)), float(ov.get("e", 999))
        partes.append(f"[{k}:v]scale={larg}:-1[ov{i}]")
        prox = f"[ovr{i}]"
        partes.append(f"{atual}[ov{i}]overlay={x}:{y}:"
                      f"enable='between(t,{s:.3f},{e:.3f})'{prox}")
        atual = prox
    return ";".join(partes), atual, ins


def _fontsdir_arg():
    """Opção fontsdir do filtro ass para libass achar a fonte enviada."""
    if CUSTOM_FONT["path"]:
        d = str(Path(CUSTOM_FONT["path"]).parent.resolve()).replace("\\", "/")
        return f":fontsdir={d}"
    return ""


def render_clip(video, start, end, ass_path, out_path, job=None,
                mvol=None, mstart=None, vvol=100, segments=None, overlays=None,
                cor=None, music_path=None):
    """Tela cheia 9:16. `segments` permite tirar trechos do meio;
    `overlays` são imagens sobrepostas com tempo de entrada/saída."""
    workdir = Path(ass_path).parent
    name = Path(ass_path).name
    fd = _fontsdir_arg()
    segs = segments or [{"s": start, "e": end}]
    tem_aud = has_audio(video)
    pre, vsrc, asrc = segments_prefix(segs, tem_aud)
    vol = mvol if mvol is not None else (job or {}).get("music_volume", 12)
    dly = mstart if mstart is not None else (job or {}).get("music_start", 0)
    minputs = music_inputs(music_path)
    midx = 1 if minputs else 1
    ovs = overlays or []
    ov_idx0 = 1 + (1 if minputs else 0)
    last = None
    for tent in range(3):
        cf, _ = color_filter(video, cor)
        base = f"{vsrc}" + (f"{cf}," if cf else "") + "crop=ih*9/16:ih,scale=1080:1920[vb]"
        og, vfim, ovins = overlay_graph("[vb]", ovs, ov_idx0)
        g = f"{pre};{base}" + (f";{og}" if og else "")
        if tent == 0:
            g += f";{vfim}ass=filename={name}{fd}[v]"
        elif tent == 1:
            g += f";{vfim}subtitles=filename={name}[v]"
        else:
            g += f";{vfim}null[v]"
        ag, alabel = audio_graph(midx, vol, dly, vvol, tem_aud,
                                 bool(minputs), fonte_audio=asrc)
        if ag:
            g += f";{ag}"
            amaps = ["-map", alabel] + (["-shortest"] if minputs else [])
        else:
            amaps = (["-map", asrc] if asrc else [])
        cmd = [FFMPEG, "-y", "-i", str(Path(video).resolve()), *minputs]
        for o in ovins:
            cmd += ["-i", o]
        cmd += ["-filter_complex", g, "-map", "[v]", *amaps,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                Path(out_path).name]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           cwd=str(workdir))
            if tent == 2 and job is not None:
                job["warning"] = ("Seu ffmpeg não suporta legendas embutidas "
                                  "(sem libass) — use o .srt. "
                                  f"Corrija com: {ffmpeg_hint()}")
            return
        except subprocess.CalledProcessError as e:
            last = e
    raise last


def render_clip_split(video, start, end, ass_path, banner_png, still_png,
                      out_path, job=None, top_fit="cover",
                      mvol=None, mstart=None, vvol=100,
                      segments=None, overlays=None, cor=None, music_path=None):
    """Layout viral: imagem/frame fixo em cima, tarja central, vídeo embaixo.
    top_fit='cover' preenche cortando; 'contain' centraliza a imagem inteira."""
    workdir = Path(ass_path).parent
    segs = segments or [{"s": start, "e": end}]
    dur = sum(float(s["e"]) - float(s["s"]) for s in segs)
    fd = _fontsdir_arg()
    # área do topo: 0-700 (imagem de apoio ou frame congelado)
    if top_fit == "contain":     # imagem inteira, centralizada, fundo preto
        top_filter = ("scale=1080:700:force_original_aspect_ratio=decrease,"
                      "pad=1080:700:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    else:                        # preenche a área cortando o excesso
        top_filter = ("scale=1080:700:force_original_aspect_ratio=increase,"
                      "crop=1080:700,setsar=1")

    tem_aud = has_audio(video)
    pre, vsrc, asrc = segments_prefix(segs, tem_aud)
    vol = mvol if mvol is not None else (job or {}).get("music_volume", 12)
    dly = mstart if mstart is not None else (job or {}).get("music_start", 0)
    minputs = music_inputs(music_path)
    ovs = overlays or []
    ov_idx0 = 3 + (1 if minputs else 0)

    def graph(with_ass):
        # vídeo embaixo (700-1920), tarja flutua sobre a emenda em y=700
        cf, _ = color_filter(video, cor)
        g = (f"{pre};"
             f"{vsrc}" + (f"{cf}," if cf else "") +
             "scale=1080:1220:force_original_aspect_ratio=increase,"
             "crop=1080:1220,setsar=1[bot];"
             f"[1:v]{top_filter}[top];"
             f"color=black:s=1080x1920:d={dur:.3f}[bg];"
             "[bg][top]overlay=0:0[t1];"
             "[t1][bot]overlay=0:700[t2];"
             "[t2][2:v]overlay=(W-w)/2:700-h/2[t3]")
        og, vfim, ovins = overlay_graph("[t3]", ovs, ov_idx0)
        if og:
            g += f";{og}"
        g += (f";{vfim}ass=filename={Path(ass_path).name}{fd}[v]" if with_ass
              else f";{vfim}null[v]")
        return g, ovins

    agraph, alabel = audio_graph(3, vol, dly, vvol, tem_aud, bool(minputs),
                                 fonte_audio=asrc)
    last = None
    for with_ass in (True, False):
        g, ovins = graph(with_ass)
        if agraph:             # música entra como 4ª entrada (índice 3)
            g = f"{g.rstrip(';')};{agraph}"
            amaps = ["-map", alabel] + (["-shortest"] if minputs else [])
        else:
            amaps = (["-map", asrc] if asrc else [])
        cmd = [FFMPEG, "-y",
               "-i", str(Path(video).resolve()),
               "-i", Path(still_png).name, "-i", Path(banner_png).name,
               *minputs]
        for o in ovins:
            cmd += ["-i", o]
        cmd += ["-filter_complex", g,
                "-map", "[v]", *amaps,
               "-c:v", "libx264", "-preset", "fast", "-crf", "20",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
               Path(out_path).name]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           cwd=str(workdir))
            if not with_ass and job is not None:
                job["warning"] = ("Seu ffmpeg não suporta legendas embutidas "
                                  "(sem libass) — cortes sem legenda no vídeo. "
                                  f"Corrija com: {ffmpeg_hint()}")
            return
        except subprocess.CalledProcessError as e:
            last = e
    raise last


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def sanitize_clips(clips, total, min_len, max_len):
    """Garante cortes válidos e com duração adequada para redes sociais.
    Estica cortes curtos até `min_len` (a IA costuma devolver trechos curtos)."""
    target = max(min_len, 20)          # nunca menos que ~20s
    out = []
    for c in clips:
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        s = max(0.0, s)
        e = min(max(e, s), total)
        dur = e - s
        # muito curto -> estende para a duração-alvo (equilibrado nos 2 lados)
        if dur < target:
            falta = target - dur
            s = max(0.0, s - falta * 0.35)
            e = min(total, s + target)
            s = max(0.0, e - target)   # reajusta se bateu no fim
        # muito longo -> limita ao máximo
        if e - s > max_len:
            e = s + max_len
        if e - s < 5:                  # vídeo curto demais para o alvo
            continue
        c["start"], c["end"] = round(s, 2), round(e, 2)
        for k, v in [("title", "Corte"), ("hook", ""), ("development", ""),
                     ("cta", ""), ("score", 50), ("reason", ""),
                     ("headline", "")]:
            c.setdefault(k, v)
        if not c["headline"]:
            c["headline"] = (c["hook"] or c["title"])[:42]
        out.append(c)
    return out


def run_pipeline(job):
    try:
        if job.get("upload_path"):
            set_status(job, "preparando", 8, "Preparando o arquivo enviado")
            video = Path(job["upload_path"])
            if not video.exists():
                raise RuntimeError("Arquivo enviado não encontrado.")
            job.setdefault("video_title", video.stem)
        else:
            set_status(job, "baixando", 5, "Baixando vídeo em HD (yt-dlp)")
            video = download_video(job)

        set_status(job, "transcrevendo", 30, "Transcrevendo com Whisper")
        transcript = transcribe(job, video)

        set_status(job, "analisando", 65, "Identificando padrões virais")
        clips = None
        choice = job.get("engine_choice") or CONFIG.get("engine_choice", "auto")
        if not job.get("api_key"):
            job["api_key"] = CONFIG.get("api_key") or None
        if not job.get("gemini_model"):
            job["gemini_model"] = CONFIG.get("gemini_model", "")
        if not job.get("openai_model"):
            job["openai_model"] = CONFIG.get("openai_model", "")
        # 'auto': usa API se houver chave, senão heurística.
        use_api = (choice in ("api", "gemini", "openai")
                   or (choice == "auto" and job.get("api_key")))
        use_ollama = (choice == "ollama")
        if use_api and job.get("api_key"):
            try:
                clips = analyze_with_ai(job, transcript)
            except Exception as e:
                # aviso bem visível: os cortes saem, mas sem a análise da IA
                nome = {"openai": "ChatGPT", "gemini": "Gemini"}.get(
                    choice, "IA")
                job["warning"] = (
                    f"⚠️ ATENÇÃO: a API do {nome} falhou e os cortes foram "
                    "escolhidos pela heurística (qualidade inferior). "
                    f"Motivo: {str(e)[:200]}")
                job.pop("engine", None)
        elif use_ollama:
            set_status(job, "analisando", 65,
                       "Analisando com IA local (Ollama)")
            try:
                clips = analyze_with_ollama(job, transcript)
            except Exception as e:
                job["warning"] = (f"Ollama não respondeu ({e}). "
                                  "O Ollama está aberto e o modelo baixado? "
                                  "Usando heurística por enquanto.")
                job.pop("engine", None)
        if clips is None:
            clips = analyze_heuristic(job, transcript)
            job.setdefault("engine", "heuristica")
        total = transcript["words"][-1]["e"] if transcript["words"] else 0
        clips = sanitize_clips(clips, total, job["min_len"], job["max_len"])
        if not clips:
            raise RuntimeError("Nenhum trecho viável encontrado — o vídeo tem "
                               "fala detectável? Tente outro modelo Whisper.")

        # ---- distribuição automática de músicas: pasta com várias trilhas ----
        # cada corte pega a próxima da fila (rodízio), em vez de repetir sempre
        # a mesma. `music_start_idx` deixa o rodízio continuar de onde parou
        # quando vários vídeos são processados em sequência (lote).
        music_folder = job.get("music_folder") or CONFIG.get("music_folder") or ""
        music_list = ([str(p) for p in _listar(music_folder, AUDIO_EXTS)]
                      if music_folder else [])
        giro0 = job.get("music_start_idx", 0)

        results = []
        for i, c in enumerate(clips, 1):
            set_status(job, "renderizando", 70 + int(25 * i / max(len(clips), 1)),
                       f"Renderizando corte {i}/{len(clips)}")
            cdir = CLIPS / job["id"]
            cdir.mkdir(parents=True, exist_ok=True)
            ass, srt = cdir / f"corte_{i}.ass", cdir / f"corte_{i}.srt"
            mp4 = cdir / f"corte_{i}.mp4"
            layout = job.get("layout", "split")
            clip_music = (music_list[(giro0 + i - 1) % len(music_list)]
                         if music_list else job.get("music_path"))
            if layout == "split":
                # headline vai na tarja, não na legenda ASS
                build_subs(transcript["words"], c["start"], c["end"], ass, srt,
                           template=job.get("template"))
                still = cdir / f"frame_{i}.png"
                banner = cdir / f"tarja_{i}.png"
                grab_still(video, c["start"] + (c["end"] - c["start"]) * 0.35,
                           still, cor=job.get("cor"))
                headline = c.get("headline") or c.get("title", "")
                if not EMOJI_RE.search(headline):    # garante emoji variado
                    headline += " " + pick_emoji(c.get("hook", ""), i)
                make_banner(headline, banner)
                render_clip_split(video, c["start"], c["end"], ass, banner,
                                  still, mp4, job, cor=job.get("cor"),
                                  music_path=clip_music)
            else:
                build_subs(transcript["words"], c["start"], c["end"], ass, srt,
                           headline=c.get("headline"),
                           template=job.get("template"))
                render_clip(video, c["start"], c["end"], ass, mp4, job,
                            cor=job.get("cor"),
                            music_path=clip_music)
            yt_t, yt_d = montar_publicacao(c, job)   # texto pronto p/ publicar
            results.append({**c, "mp4": f"/clips/{job['id']}/{mp4.name}",
                            "srt": f"/clips/{job['id']}/{srt.name}",
                            "duration": round(c["end"] - c["start"], 1),
                            "yt_title": yt_t, "yt_description": yt_d,
                            "musica": Path(clip_music).name if clip_music else None})
            job.setdefault("_render", {})[i] = {
                "video": str(video), "ass": str(ass), "srt": str(srt),
                "mp4": str(mp4), "layout": layout,
                "banner": str(banner) if layout == "split" else None,
                "still": str(still) if layout == "split" else None,
                "top_image": None,          # imagem de apoio (opcional)
                "top_fit": "cover",         # cover = preenche | contain = centraliza
                "music_volume": job.get("music_volume", 12),
                "music_start": 0,           # atraso da trilha dentro do corte
                "cor": job.get("cor"),
                "music_path": clip_music,
                "template": job.get("template", "sora-clean"),
                "sub_font": tpl(job.get("template")).get("font"),
                "sub_size": tpl(job.get("template")).get("size"),
                "start": c["start"], "end": c["end"],
                "headline": None if layout == "split" else c.get("headline"),
            }
        job["_words"] = transcript["words"]
        job["clips"] = results
        job["music_used_count"] = len(clips)  # o lote usa isto p/ continuar o rodízio
        set_status(job, "concluido", 100, f"{len(results)} cortes gerados")
        save_job(job)                     # projeto sobrevive a reinícios
    except subprocess.CalledProcessError as e:
        job["step"] = "erro"
        job["detail"] = (e.stderr or str(e))[-800:]
    except Exception as e:
        job["step"] = "erro"
        job["detail"] = str(e)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.post("/api/jobs")
def create_job(req: JobRequest):
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, **req.model_dump(), "step": "iniciando",
           "progress": 0, "detail": "", "clips": []}
    JOBS[job_id] = job
    threading.Thread(target=run_pipeline, args=(job,), daemon=True).start()
    return {"id": job_id}


@app.post("/api/jobs/upload")
async def create_job_upload(
    file: UploadFile = File(...),
    api_key: str = Form(""),
    num_clips: int = Form(3),
    min_len: int = Form(25),
    max_len: int = Form(75),
    whisper_model: str = Form("small"),
    layout: str = Form("split"),
    engine_choice: str = Form("auto"),
    ollama_model: str = Form(""),
    gemini_model: str = Form(""),
    openai_model: str = Form(""),
    music_volume: int = Form(12),
    music_folder: str = Form(""),
    template: str = Form("sora-clean"),
):
    ext = Path(file.filename or "video.mp4").suffix.lower()
    if ext not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
        return JSONResponse({"detail": f"formato não suportado: {ext}"},
                            status_code=400)
    job_id = uuid.uuid4().hex[:8]
    dest = WORK / job_id / f"upload{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    job = {"id": job_id, "url": "", "upload_path": str(dest),
           "video_title": Path(file.filename or "video").stem,
           "api_key": api_key or None, "num_clips": num_clips,
           "min_len": min_len, "max_len": max_len,
           "whisper_model": whisper_model, "layout": layout,
           "engine_choice": engine_choice, "ollama_model": ollama_model,
           "gemini_model": gemini_model, "openai_model": openai_model,
           "music_volume": music_volume,
           "music_folder": music_folder, "template": template,
           "step": "iniciando", "progress": 0, "detail": "", "clips": []}
    JOBS[job_id] = job
    threading.Thread(target=run_pipeline, args=(job,), daemon=True).start()
    return {"id": job_id}


@app.get("/api/projects")
def listar_projetos(limite: int = 30):
    """Projetos já processados, para reabrir os cortes sem refazer tudo.

    Sem isto, sair da página (ou ir ao editor e voltar) fazia parecer que o
    trabalho tinha sumido — os cortes continuavam em disco, mas sem caminho
    de volta até eles."""
    itens = []
    for job in JOBS.values():
        if job.get("step") != "concluido" or not job.get("clips"):
            continue
        jf = WORK / job["id"] / "job.json"
        try:
            quando = jf.stat().st_mtime if jf.exists() else 0
        except Exception:
            quando = 0
        prim = (job.get("clips") or [{}])[0]
        itens.append({
            "id": job["id"],
            "titulo": job.get("video_title") or "(sem título)",
            "cortes": len(job.get("clips") or []),
            "quando": quando,
            "layout": job.get("layout", "split"),
            "origem": "lote" if job.get("origem") else
                      ("arquivo" if job.get("upload_path") else "youtube"),
            "capa": prim.get("mp4"),
        })
    itens.sort(key=lambda x: x["quando"], reverse=True)
    return {"projetos": itens[:limite], "total": len(itens)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job não encontrado"}, status_code=404)
    # projetos gerados antes do texto de publicação ganham um agora, para não
    # cair no campo errado (a antiga descrição vinha de 'reason')
    mudou = False
    for c in job.get("clips", []):
        d = c.get("yt_description") or ""
        # marcas da versão antiga (descrição comprida, estilo resumo)
        velha = ("Trecho de:" in d
                 or "segue o canal para mais cortes como este" in d)
        if not c.get("yt_title") or not d or velha:
            if velha:
                c.pop("yt_description", None)      # força regerar mais curta
            c["yt_title"], c["yt_description"] = montar_publicacao(c, job)
            mudou = True
    if mudou:
        save_job(job)
    return {k: v for k, v in job.items()
            if k != "api_key" and not k.startswith("_")}


class SubtitleEdit(BaseModel):
    lines: List[str]


@app.get("/api/jobs/{job_id}/clips/{idx}/subtitle")
def get_subtitle(job_id: str, idx: int):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado (gere o corte "
                             "nesta sessão do servidor)"}, status_code=404)
    lines = group_lines(job["_words"], r["start"], r["end"],
                        groups=r.get("groups"))
    return {"lines": [" ".join(w["w"] for w in ln) for ln in lines]}


@app.post("/api/jobs/{job_id}/clips/{idx}/subtitle")
def edit_subtitle(job_id: str, idx: int, edit: SubtitleEdit):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    old_lines = group_lines(job["_words"], r["start"], r["end"],
                            groups=r.get("groups"))
    if len(edit.lines) != len(old_lines):
        return JSONResponse({"detail": "número de linhas não confere"},
                            status_code=400)
    # redistribui o tempo original de cada linha entre as palavras editadas
    new_words = []
    for ln, text in zip(old_lines, edit.lines):
        tokens = text.split()
        if not tokens:
            continue
        s, e = ln[0]["s"], ln[-1]["e"]
        total = sum(len(t) for t in tokens) or 1
        cur = s
        for t in tokens:
            dt = (e - s) * len(t) / total
            new_words.append({"w": t, "s": round(cur, 3),
                              "e": round(cur + dt, 3)})
            cur += dt
    others = [w for w in job["_words"]
              if not (r["start"] <= w["s"] < r["end"])]
    job["_words"] = sorted(others + new_words, key=lambda w: w["s"])
    # regrava legenda e re-renderiza o corte
    ass, srt = Path(r["ass"]), Path(r["srt"])
    build_subs(job["_words"], r["start"], r["end"], ass, srt,
               headline=r.get("headline"))
    return rerender_clip(job, r)


def rerender_clip(job, r):
    """Re-renderiza um corte a partir do seu estado (r), mantendo imagem de
    apoio e música de fundo."""
    ass = Path(r["ass"])
    mvol = r.get("music_volume", job.get("music_volume", 12))
    mstart = r.get("music_start", 0)
    vvol = r.get("video_volume", 100)
    segs = r.get("segments") or [{"s": r["start"], "e": r["end"]}]
    ovs = r.get("overlays") or []
    cor = r.get("cor") or job.get("cor")
    mpath = r.get("music_path")
    try:
        if r["layout"] == "split":
            top = r.get("top_image") or r["still"]
            render_clip_split(r["video"], r["start"], r["end"], ass,
                              r["banner"], top, r["mp4"], job,
                              top_fit=r.get("top_fit", "cover"),
                              mvol=mvol, mstart=mstart, vvol=vvol,
                              segments=segs, overlays=ovs, cor=cor,
                              music_path=mpath)
        else:
            render_clip(r["video"], r["start"], r["end"], ass, r["mp4"], job,
                        mvol=mvol, mstart=mstart, vvol=vvol,
                        segments=segs, overlays=ovs, cor=cor,
                        music_path=mpath)
    except subprocess.CalledProcessError as e:
        return JSONResponse({"detail": (e.stderr or str(e))[-500:]},
                            status_code=500)
    return {"ok": True}


# --------------------------------------------------------------------------
# Lote: processa uma pasta inteira (ex.: pasta do Google Drive para Desktop)
# --------------------------------------------------------------------------

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac")
BATCH = {"ativo": False, "fila": [], "feitos": [], "atual": None,
         "erros": [], "cancelar": False}


class BatchRequest(BaseModel):
    pasta: str                          # pasta com os vídeos (Drive Desktop)
    pasta_musicas: str = ""             # pasta com as trilhas (rodízio)
    cortes_por_video: int = 1
    min_len: int = 25
    max_len: int = 75
    whisper_model: str = "small"
    layout: str = "split"
    engine_choice: str = "auto"
    api_key: Optional[str] = None
    gemini_model: str = ""
    openai_model: str = ""
    ollama_model: str = ""
    music_volume: int = 12
    auto_hdr: bool = True
    brilho: float = 0.0
    contraste: float = 1.0
    saturacao: float = 1.0
    temperatura: int = 0
    template: str = "sora-clean"
    reprocessar: bool = False           # refazer vídeos já processados


def _listar(pasta, exts):
    p = Path(os.path.expanduser(pasta))
    if not p.is_dir():
        return []
    return sorted([f for f in p.iterdir()
                   if f.is_file() and f.suffix.lower() in exts
                   and not f.name.startswith(".")])


@app.get("/api/batch/scan")
def batch_scan(pasta: str = "", pasta_musicas: str = ""):
    """Mostra o que existe nas pastas antes de começar."""
    vids = _listar(pasta, VIDEO_EXTS) if pasta else []
    muss = _listar(pasta_musicas, AUDIO_EXTS) if pasta_musicas else []
    feitos = {j.get("origem") for j in JOBS.values() if j.get("origem")}
    return {
        "pasta_ok": bool(pasta) and Path(os.path.expanduser(pasta)).is_dir(),
        "musicas_ok": (bool(pasta_musicas)
                       and Path(os.path.expanduser(pasta_musicas)).is_dir()),
        "videos": [{"nome": f.name, "mb": round(f.stat().st_size / 1e6, 1),
                    "feito": str(f) in feitos} for f in vids],
        "musicas": [m.name for m in muss],
    }


class ConfigIn(BaseModel):
    api_key: Optional[str] = None
    engine_choice: Optional[str] = None
    gemini_model: Optional[str] = None
    ollama_model: Optional[str] = None
    openai_model: Optional[str] = None
    template: Optional[str] = None
    music_folder: Optional[str] = None


@app.get("/api/config")
def get_config():
    k = (CONFIG.get("api_key") or "").strip()
    if eh_chave_gemini(k):
        provedor = "Gemini (Google)"
    elif k.startswith("sk-ant"):
        provedor = "Claude (Anthropic)"
    elif eh_chave_openai(k):
        provedor = "ChatGPT (OpenAI)"
    else:
        provedor = ""
    return {**{x: y for x, y in CONFIG.items() if x != "api_key"},
            "versao": VERSAO,
            "tem_chave": bool(k), "provedor": provedor,
            "chave_mascarada": (k[:8] + "…" + k[-4:]) if len(k) > 14 else
                               ("•" * len(k) if k else "")}


@app.post("/api/config")
def set_config(c: ConfigIn):
    for campo, valor in c.model_dump().items():
        if valor is not None:
            CONFIG[campo] = valor.strip() if isinstance(valor, str) else valor
    save_config()
    return get_config()


@app.post("/api/config/test")
def test_config():
    """Faz uma chamada real de teste e diz exatamente o que aconteceu."""
    k = (CONFIG.get("api_key") or "").strip()
    if not k:
        return {"ok": False, "msg": "Nenhuma chave salva."}
    modelo = CONFIG.get("gemini_model") or "gemini-2.5-flash"
    if eh_chave_gemini(k):
        try:
            r = _gemini_call(k, modelo, 'Responda apenas: {"ok":true}')
            if r.status_code == 200:
                texto, problema = texto_da_resposta(r.json())
                if problema:      # 200 mas sem texto útil
                    return {"ok": False, "provedor": "gemini",
                            "msg": f"A chave funciona, mas {problema}."}
                return {"ok": True, "msg": f"Gemini respondeu ({modelo}).",
                        "provedor": "gemini"}
            det = erro_google(r)
            dicas = {400: "Requisição recusada — veja a mensagem do Google abaixo.",
                     401: "Chave inválida ou mal copiada.",
                     403: "Chave sem permissão — a API está ativada no projeto?",
                     404: f"Modelo '{modelo}' não existe para esta chave.",
                     429: "Limite atingido — pode ser ritmo ou saldo/plano."}
            return {"ok": False, "provedor": "gemini",
                    "msg": f"HTTP {r.status_code}. "
                           f"{dicas.get(r.status_code, '')} {det}"}
        except Exception as e:
            return {"ok": False, "msg": f"Falha de conexão: {e}"}
    if k.startswith("sk-ant"):
        try:
            import anthropic
            anthropic.Anthropic(api_key=k).messages.create(
                model="claude-sonnet-5", max_tokens=8,
                messages=[{"role": "user", "content": "oi"}])
            return {"ok": True, "msg": "Claude respondeu.", "provedor": "claude"}
        except Exception as e:
            return {"ok": False, "msg": f"Claude recusou: {str(e)[:220]}"}
    if eh_chave_openai(k):
        modelo_gpt = CONFIG.get("openai_model") or "gpt-5.6-terra"
        try:
            r = _openai_call(k, modelo_gpt, 'Responda apenas: {"ok":true}',
                             max_tokens=2000)
            if r.status_code == 200:
                texto, problema = texto_openai(r.json())
                if problema:
                    return {"ok": False, "provedor": "openai",
                            "msg": f"A chave funciona, mas {problema}."}
                return {"ok": True, "provedor": "openai",
                        "msg": f"ChatGPT respondeu ({modelo_gpt})."}
            det = erro_openai(r)
            dicas = {400: "Requisição recusada — veja a mensagem abaixo.",
                     401: "Chave inválida ou revogada.",
                     403: "Chave sem permissão para este modelo.",
                     404: f"Modelo '{modelo_gpt}' não existe para esta chave.",
                     429: "Limite atingido — pode ser ritmo ou créditos."}
            return {"ok": False, "provedor": "openai",
                    "msg": f"HTTP {r.status_code}. "
                           f"{dicas.get(r.status_code, '')} {det}"}
        except Exception as e:
            return {"ok": False, "msg": f"Falha de conexão: {str(e)[:200]}"}
    # formato desconhecido: tenta como Gemini em vez de rejeitar de cara
    try:
        r = _gemini_call(k, modelo, 'Responda apenas: {"ok":true}')
        if r.status_code == 200:
            return {"ok": True, "provedor": "gemini",
                    "msg": f"Gemini respondeu ({modelo})."}
        return {"ok": False, "provedor": "gemini",
                "msg": f"HTTP {r.status_code} — a chave não foi aceita pelo "
                       f"Gemini. Confira em aistudio.google.com/apikey."}
    except Exception as e:
        return {"ok": False, "msg": f"Falha ao testar: {str(e)[:200]}"}


@app.get("/api/gemini/diag")
def gemini_diag():
    """Testa a chave do Gemini em etapas e diz exatamente onde trava.

    Feito para não precisar mais adivinhar: separa 'a chave não vale' de
    'a chave vale mas o modelo escolhido não existe' e de 'respondeu mas
    veio vazio'."""
    import requests
    k = (CONFIG.get("api_key") or "").strip()
    out = {"etapas": [], "conclusao": "", "ok": False}

    def passo(nome, ok, detalhe=""):
        out["etapas"].append({"nome": nome, "ok": ok, "detalhe": detalhe})

    if not k:
        out["conclusao"] = "Nenhuma chave salva. Cole a chave e clique em Salvar."
        return out
    passo("Chave salva", True,
          f"{k[:8]}…{k[-4:]} ({'AQ.' if k.startswith('AQ.') else 'AIza'} - "
          f"{len(k)} caracteres)")

    # 1) a chave é aceita? listar modelos é a chamada mais barata que existe
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": k}, timeout=30)
    except Exception as e:
        passo("Conexão com o Google", False, str(e)[:200])
        out["conclusao"] = ("Não consegui nem alcançar o Google. Verifique sua "
                            "internet, VPN ou firewall.")
        return out

    if r.status_code != 200:
        passo("Chave aceita pelo Google", False, erro_google(r))
        if r.status_code in (401, 403):
            out["conclusao"] = ("A chave foi recusada. Gere outra em "
                                "aistudio.google.com/apikey e confira se a "
                                "'Generative Language API' está ativada no projeto.")
        elif r.status_code == 429:
            out["conclusao"] = ("Limite atingido já na listagem de modelos — "
                                "isso é cota/saldo do projeto, não do nosso app.")
        else:
            out["conclusao"] = "O Google recusou a chave. Veja a mensagem acima."
        return out

    disponiveis = [m.get("name", "").replace("models/", "")
                   for m in (r.json().get("models") or [])
                   if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    passo("Chave aceita pelo Google", True,
          f"{len(disponiveis)} modelos disponíveis")

    # 2) o modelo configurado existe para esta chave?
    modelo = CONFIG.get("gemini_model") or "gemini-2.5-flash"
    tem = modelo in disponiveis
    passo(f"Modelo '{modelo}' disponível", tem,
          "" if tem else "escolha outro na lista de modelos")
    out["modelos_disponiveis"] = sorted(disponiveis)[:40]
    if not tem:
        sugestao = next((m for m in disponiveis if "flash" in m), None)
        out["conclusao"] = (f"A chave funciona, mas o modelo '{modelo}' não está "
                            f"liberado para ela."
                            + (f" Troque para '{sugestao}'." if sugestao else ""))
        return out

    # 3) chamada real de geração
    try:
        g = _gemini_call(k, modelo, 'Responda apenas: {"ok":true}')
    except Exception as e:
        passo("Chamada de geração", False, str(e)[:200])
        out["conclusao"] = f"Falha na chamada: {e}"
        return out

    if g.status_code != 200:
        passo("Chamada de geração", False, erro_google(g))
        if g.status_code == 429:
            if erro_permanente_429(g):
                out["sem_saldo"] = True
                out["conclusao"] = (
                    "💳 A CHAVE ESTÁ CERTA — o que falta é saldo no projeto. "
                    "Dica: o Gemini tem camada gratuita, mas só em projeto SEM "
                    "faturamento ativo. Crie uma chave nova em "
                    "aistudio.google.com/apikey escolhendo um projeto novo, sem "
                    "vincular cartão — aí a cota grátis volta a valer. "
                    "Ou use o motor Ollama/Heurística, que são grátis.")
            else:
                out["conclusao"] = ("Excesso de requisições por minuto. "
                                    "Espere um pouco e teste de novo.")
        else:
            out["conclusao"] = "O Google recusou a geração. Veja a mensagem acima."
        return out

    j = g.json()
    texto, problema = texto_da_resposta(j)
    uso = j.get("usageMetadata") or {}
    detalhe_uso = (f"tokens: entrada {uso.get('promptTokenCount', '?')}, "
                   f"saída {uso.get('candidatesTokenCount', '?')}, "
                   f"raciocínio {uso.get('thoughtsTokenCount', 0)}")
    if problema:
        passo("Resposta com conteúdo", False, f"{problema} · {detalhe_uso}")
        out["conclusao"] = (f"A chave e o modelo funcionam, mas {problema}. "
                            "Isso é ajuste do nosso lado, não da sua conta.")
        return out

    passo("Resposta com conteúdo", True, f"{texto[:60]} · {detalhe_uso}")
    out["ok"] = True
    out["conclusao"] = (f"Tudo certo! Chave válida, modelo '{modelo}' respondendo. "
                        "Pode gerar os cortes normalmente.")
    return out


@app.get("/api/openai/diag")
def openai_diag():
    """Mesmo diagnóstico em etapas, para a chave da OpenAI (ChatGPT)."""
    import requests
    k = (CONFIG.get("api_key") or "").strip()
    out = {"etapas": [], "conclusao": "", "ok": False}

    def passo(nome, ok, detalhe=""):
        out["etapas"].append({"nome": nome, "ok": ok, "detalhe": detalhe})

    if not k:
        out["conclusao"] = "Nenhuma chave salva. Cole a chave e clique em Salvar."
        return out
    if not eh_chave_openai(k):
        out["conclusao"] = ("A chave salva não é da OpenAI (deve começar com "
                            "sk-). Use o diagnóstico do Gemini.")
        return out
    passo("Chave salva", True, f"{k[:10]}…{k[-4:]} ({len(k)} caracteres)")

    # 1) a chave é aceita?
    try:
        r = requests.get("https://api.openai.com/v1/models",
                         headers={"Authorization": f"Bearer {k}"}, timeout=30)
    except Exception as e:
        passo("Conexão com a OpenAI", False, str(e)[:200])
        out["conclusao"] = ("Não consegui alcançar a OpenAI. Verifique "
                            "internet, VPN ou firewall.")
        return out

    if r.status_code != 200:
        passo("Chave aceita pela OpenAI", False, erro_openai(r))
        out["conclusao"] = ("A chave foi recusada. Gere outra em "
                            "platform.openai.com/api-keys e confira se o "
                            "projeto tem créditos."
                            if r.status_code in (401, 403) else
                            "A OpenAI recusou a chave. Veja a mensagem acima.")
        return out

    disponiveis = sorted({m.get("id", "") for m in (r.json().get("data") or [])})
    passo("Chave aceita pela OpenAI", True,
          f"{len(disponiveis)} modelos disponíveis")

    modelo = CONFIG.get("openai_model") or "gpt-5.6-terra"
    tem = modelo in disponiveis
    passo(f"Modelo '{modelo}' disponível", tem,
          "" if tem else "escolha outro na lista de modelos")
    out["modelos_disponiveis"] = [m for m in disponiveis
                                  if m.startswith(("gpt-", "o1", "o3", "o4"))][:40]
    if not tem:
        sugestao = next((m for m in disponiveis if m.startswith("gpt-5")), None)
        out["conclusao"] = (f"A chave funciona, mas o modelo '{modelo}' não está "
                            f"liberado para ela."
                            + (f" Troque para '{sugestao}'." if sugestao else ""))
        return out

    # 2) geração real
    try:
        g = _openai_call(k, modelo, 'Responda apenas: {"ok":true}',
                         max_tokens=2000)
    except Exception as e:
        passo("Chamada de geração", False, str(e)[:200])
        out["conclusao"] = f"Falha na chamada: {e}"
        return out

    if g.status_code != 200:
        det = erro_openai(g)
        passo("Chamada de geração", False, det)
        if g.status_code == 429:
            if erro_permanente_429(g):
                out["sem_saldo"] = True
                out["conclusao"] = (
                    "💳 A CHAVE ESTÁ CERTA — o que falta é saldo. A OpenAI "
                    "cobra por uso e não tem plano grátis: é preciso comprar "
                    "créditos em platform.openai.com/settings/organization/billing "
                    "(a assinatura do ChatGPT Plus NÃO vale para a API). "
                    "Enquanto isso, use o motor Ollama ou Heurística, que são "
                    "grátis e funcionam sem chave.")
            else:
                out["conclusao"] = ("Excesso de requisições por minuto. "
                                    "Espere um pouco e teste de novo.")
        else:
            out["conclusao"] = "A OpenAI recusou a geração. Veja a mensagem acima."
        return out

    j = g.json()
    texto, problema = texto_openai(j)
    uso = j.get("usage") or {}
    det_uso = (f"tokens: entrada {uso.get('prompt_tokens', '?')}, "
               f"saída {uso.get('completion_tokens', '?')}")
    if problema:
        passo("Resposta com conteúdo", False, f"{problema} · {det_uso}")
        out["conclusao"] = (f"A chave e o modelo funcionam, mas {problema}. "
                            "Isso é ajuste do nosso lado, não da sua conta.")
        return out

    passo("Resposta com conteúdo", True, f"{texto[:60]} · {det_uso}")
    out["ok"] = True
    out["conclusao"] = (f"Tudo certo! Chave válida, modelo '{modelo}' "
                        "respondendo. Pode gerar os cortes normalmente.")
    return out


def _ordenar_modelos(nomes, preferidos):
    """Coloca os modelos mais indicados primeiro, o resto em ordem alfabética."""
    top = [n for p in preferidos for n in nomes if n == p]
    resto = sorted(n for n in nomes if n not in top)
    return top + resto


@app.get("/api/models")
def listar_modelos():
    """Modelos realmente liberados para a chave salva.

    Existe porque uma lista fixa na tela envelhece e faz o usuário escolher um
    modelo que a conta dele não tem — erro que parece bug do app."""
    import requests
    k = (CONFIG.get("api_key") or "").strip()
    if not k:
        return {"ok": False, "erro": "Nenhuma chave salva.", "modelos": []}
    try:
        if eh_chave_openai(k):
            r = requests.get("https://api.openai.com/v1/models",
                             headers={"Authorization": f"Bearer {k}"}, timeout=30)
            if r.status_code != 200:
                return {"ok": False, "erro": erro_openai(r), "modelos": []}
            todos = [m.get("id", "") for m in (r.json().get("data") or [])]
            # só os de conversa (fora embeddings, áudio, imagem…)
            uteis = [m for m in todos
                     if m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))
                     and not any(x in m for x in
                                 ("audio", "realtime", "tts", "transcribe",
                                  "image", "search", "embedding", "moderation",
                                  "codex", "instruct"))]
            pref = ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6",
                    "gpt-5-mini", "gpt-5", "gpt-5-nano", "gpt-4o-mini", "gpt-4o"]
            return {"ok": True, "provedor": "openai",
                    "atual": CONFIG.get("openai_model", ""),
                    "modelos": _ordenar_modelos(uteis, pref)}

        if eh_chave_gemini(k):
            r = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": k}, timeout=30)
            if r.status_code != 200:
                return {"ok": False, "erro": erro_google(r), "modelos": []}
            uteis = [m.get("name", "").replace("models/", "")
                     for m in (r.json().get("models") or [])
                     if "generateContent" in
                     (m.get("supportedGenerationMethods") or [])]
            uteis = [m for m in uteis if not any(
                x in m for x in ("embedding", "aqa", "vision", "tts", "image"))]
            pref = ["gemini-2.5-flash", "gemini-2.5-flash-lite",
                    "gemini-2.5-pro", "gemini-2.0-flash"]
            return {"ok": True, "provedor": "gemini",
                    "atual": CONFIG.get("gemini_model", ""),
                    "modelos": _ordenar_modelos(uteis, pref)}
    except Exception as e:
        return {"ok": False, "erro": str(e)[:200], "modelos": []}
    return {"ok": False, "erro": "Formato de chave não reconhecido.",
            "modelos": []}


@app.get("/api/ia/diag")
def ia_diag():
    """Roda o diagnóstico certo conforme o formato da chave salva."""
    k = (CONFIG.get("api_key") or "").strip()
    if eh_chave_openai(k):
        return {**openai_diag(), "provedor": "ChatGPT (OpenAI)"}
    return {**gemini_diag(), "provedor": "Gemini (Google)"}


@app.get("/api/diag")
def diagnostico(pasta: str = "", pasta_musicas: str = ""):
    """Verifica cada peça do sistema e devolve o que está ou não funcionando."""
    d = {}
    # ffmpeg e filtros necessários
    try:
        v = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True)
        d["ffmpeg"] = {"ok": v.returncode == 0, "caminho": FFMPEG,
                       "versao": (v.stdout or "").splitlines()[0][:60]}
        fl = subprocess.run([FFMPEG, "-filters"], capture_output=True,
                            text=True).stdout
        d["ffmpeg"]["legenda_ass"] = bool(re.search(r"^ *[A-Z.]+ +ass +", fl, re.M))
        d["ffmpeg"]["tonemap_hdr"] = ("zscale" in fl and "tonemap" in fl)
    except Exception as e:
        d["ffmpeg"] = {"ok": False, "erro": str(e)}
    # ffprobe
    try:
        p = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True)
        d["ffprobe"] = {"ok": p.returncode == 0}
    except Exception as e:
        d["ffprobe"] = {"ok": False, "erro": str(e)}
    # whisper
    try:
        import faster_whisper                                   # noqa
        d["whisper"] = {"ok": True,
                        "versao": getattr(faster_whisper, "__version__", "?")}
    except Exception as e:
        d["whisper"] = {"ok": False, "erro": str(e)[:200]}
    # ollama
    try:
        import requests
        rr = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        mods = [m["name"] for m in rr.json().get("models", [])]
        d["ollama"] = {"ok": True, "modelos": mods[:8]}
    except Exception as e:
        d["ollama"] = {"ok": False, "erro": f"não respondeu ({type(e).__name__})"}
    # espaço em disco
    try:
        u = shutil.disk_usage(str(BASE))
        d["disco"] = {"livre_gb": round(u.free / 1e9, 1),
                      "total_gb": round(u.total / 1e9, 1),
                      "ok": u.free > 3e9}
    except Exception as e:
        d["disco"] = {"ok": False, "erro": str(e)}
    # pasta e primeiro vídeo
    if pasta:
        p = Path(os.path.expanduser(pasta))
        info = {"caminho": str(p), "existe": p.is_dir()}
        if p.is_dir():
            vids = _listar(pasta, VIDEO_EXTS)
            info["videos"] = len(vids)
            if vids:
                v0 = vids[0]
                info["primeiro"] = v0.name
                info["mb"] = round(v0.stat().st_size / 1e6, 1)
                try:
                    with open(v0, "rb") as f:
                        f.read(1024)
                    info["leitura"] = True
                except Exception as e:
                    info["leitura"] = False
                    info["erro_leitura"] = str(e)[:150]
                info["duracao_s"] = video_duration(v0)
                info["tem_audio"] = has_audio(v0)
                info["hdr"] = is_hdr(v0)
        d["pasta"] = info
    if pasta_musicas:
        d["musicas"] = {"existe": Path(os.path.expanduser(pasta_musicas)).is_dir(),
                        "qtd": len(_listar(pasta_musicas, AUDIO_EXTS))}
    d["lote"] = {k: v for k, v in BATCH.items() if k != "cancelar"}
    d["jobs_em_memoria"] = len(JOBS)
    return d


@app.get("/api/batch/status")
def batch_status():
    st = {k: v for k, v in BATCH.items() if k != "cancelar"}
    at = st.get("atual")
    if at and at.get("job") and at["job"] in JOBS:
        j = JOBS[at["job"]]              # progresso ao vivo do vídeo atual
        st["atual"] = {**at, "step": j.get("step", "?"),
                       "progress": j.get("progress", 0),
                       "detail": j.get("detail", "")}
    return st


@app.post("/api/batch/cancel")
def batch_cancel():
    BATCH["cancelar"] = True
    return {"ok": True}


def _rodar_lote(req: BatchRequest):
    vids = _listar(req.pasta, VIDEO_EXTS)
    feitos = {j.get("origem") for j in JOBS.values() if j.get("origem")}
    if not req.reprocessar:
        vids = [v for v in vids if str(v) not in feitos]
    cor = {"auto_hdr": req.auto_hdr, "brilho": req.brilho,
           "contraste": req.contraste, "saturacao": req.saturacao,
           "temperatura": req.temperatura}
    BATCH.update({"ativo": True, "cancelar": False, "erros": [],
                  "fila": [v.name for v in vids], "feitos": [], "atual": None})
    giro = 0     # rodízio das músicas: continua entre vídeos E entre cortes
    for v in vids:
        if BATCH["cancelar"]:
            break
        job_id = uuid.uuid4().hex[:8]
        mb = round(v.stat().st_size / 1e6)
        BATCH["atual"] = {"nome": v.name, "job": job_id, "step": "preparando",
                          "progress": 2, "detail": f"{mb} MB"}
        # lê direto da pasta de origem — não copia (economiza disco e tempo)
        dest = v
        (WORK / job_id).mkdir(parents=True, exist_ok=True)
        job = {"id": job_id, "url": "", "upload_path": str(dest),
               "origem": str(v), "video_title": v.stem,
               "api_key": req.api_key or None,
               "num_clips": max(1, req.cortes_por_video),
               "min_len": req.min_len, "max_len": req.max_len,
               "whisper_model": req.whisper_model, "layout": req.layout,
               "engine_choice": req.engine_choice,
               "gemini_model": req.gemini_model,
               "openai_model": req.openai_model,
               "ollama_model": req.ollama_model,
               "music_volume": req.music_volume,
               "cor": cor, "music_folder": req.pasta_musicas,
               "music_start_idx": giro,        # continua o rodízio de onde parou
               "template": req.template,
               "step": "iniciando", "progress": 0, "detail": "", "clips": []}
        JOBS[job_id] = job
        BATCH["atual"] = {"nome": v.name, "job": job_id,
                          "step": "iniciando", "progress": 3}
        try:
            run_pipeline(job)                  # síncrono: um vídeo por vez
        except Exception as e:                 # falha inesperada -> mostra tudo
            job["step"] = "erro"
            job["detail"] = f"{e}\n{traceback.format_exc()[-500:]}"
        giro += job.get("music_used_count", 0) or max(1, req.cortes_por_video)
        if job["step"] == "erro":
            BATCH["erros"].append(f"{v.name}: {job.get('detail', '')[:160]}")
        else:
            musicas = sorted({c.get("musica") for c in job.get("clips", [])
                             if c.get("musica")})
            BATCH["feitos"].append({
                "nome": v.name, "job": job_id,
                "cortes": len(job.get("clips", [])),
                "musica": ", ".join(musicas) if musicas else None})
        if v.name in BATCH["fila"]:
            BATCH["fila"].remove(v.name)
    BATCH["ativo"] = False
    BATCH["atual"] = None


@app.post("/api/batch")
def batch_start(req: BatchRequest):
    if BATCH["ativo"]:
        return JSONResponse({"detail": "já existe um lote em andamento"},
                            status_code=409)
    if not Path(os.path.expanduser(req.pasta)).is_dir():
        return JSONResponse({"detail": "pasta de vídeos não encontrada"},
                            status_code=400)
    threading.Thread(target=_rodar_lote, args=(req,), daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------------------
# Editor visual (preview ao vivo + timeline com miniaturas)
# --------------------------------------------------------------------------

class TLLine(BaseModel):
    s: float
    e: float
    text: Optional[str] = None          # texto editado do bloco (opcional)


class TimelineEdit(BaseModel):
    start: Optional[float] = None       # início do corte no vídeo original
    end: Optional[float] = None
    lines: List[TLLine] = []            # tempos relativos ao início do corte
    music_start: float = 0
    music_volume: int = 12
    video_volume: int = 100             # volume da voz/áudio original


def _rel_url(p, base, prefixo):
    """Converte caminho de arquivo em URL servida pelo app."""
    try:
        return f"{prefixo}/{Path(p).resolve().relative_to(base.resolve())}"
    except Exception:
        return None


@app.get("/api/jobs/{job_id}/source")
def stream_source(job_id: str, request: Request):
    """Serve o vídeo original mesmo fora da pasta do app (lote lê do Drive),
    com suporte a Range para o player poder buscar."""
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(1) or {}
    p = Path(r.get("video") or job.get("upload_path", "") if job else "")
    if not p or not p.exists():
        return JSONResponse({"detail": "vídeo não encontrado"}, status_code=404)
    tam = p.stat().st_size
    faixa = request.headers.get("range")
    tipo = "video/mp4" if p.suffix.lower() != ".webm" else "video/webm"
    if not faixa:
        return FileResponse(p, media_type=tipo)
    m = re.match(r"bytes=(\d+)-(\d*)", faixa)
    ini = int(m.group(1)) if m else 0
    fim = int(m.group(2)) if (m and m.group(2)) else min(ini + 4_000_000,
                                                        tam - 1)
    fim = min(fim, tam - 1)

    def gerar():
        with open(p, "rb") as f:
            f.seek(ini)
            restante = fim - ini + 1
            while restante > 0:
                pedaco = f.read(min(262144, restante))
                if not pedaco:
                    break
                restante -= len(pedaco)
                yield pedaco

    return StreamingResponse(gerar(), status_code=206, media_type=tipo,
                             headers={"Content-Range": f"bytes {ini}-{fim}/{tam}",
                                      "Accept-Ranges": "bytes",
                                      "Content-Length": str(fim - ini + 1)})


@app.get("/api/jobs/{job_id}/clips/{idx}/editor")
def editor_state(job_id: str, idx: int):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado (gere-o nesta "
                             "sessão do servidor)"}, status_code=404)
    cs, ce = r["start"], r["end"]
    words = [w for w in job["_words"] if cs - 2 <= w["s"] <= ce + 2]
    total = video_duration(r["video"]) or (
        job["_words"][-1]["e"] if job["_words"] else ce)
    cl = job["clips"][idx - 1] if len(job.get("clips", [])) >= idx else {}
    return {
        "video_url": (_rel_url(r["video"], WORK, "/work")
                      or f"/api/jobs/{job_id}/source"),
        "start": cs, "end": ce, "duration": round(ce - cs, 3),
        "video_total": round(total, 2),
        "layout": r["layout"], "top_fit": r.get("top_fit", "cover"),
        "headline": cl.get("headline", ""),
        "title": cl.get("title", f"Corte {idx}"),
        "banner_url": _rel_url(r.get("banner"), CLIPS, "/clips"),
        "top_url": _rel_url(r.get("top_image") or r.get("still"), CLIPS, "/clips"),
        "mp4_url": _rel_url(r["mp4"], CLIPS, "/clips"),
        "words": [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in words],
        "lines": [{"text": " ".join(w["w"] for w in ln),
                   "s": round(ln[0]["s"] - cs, 3),
                   "e": round(min(ln[-1]["e"], ce) - cs, 3)}
                  for ln in group_lines(job["_words"], cs, ce,
                                        groups=r.get("groups"))],
        "music": {"name": BG_MUSIC["name"],
                  "url": (f"/musicfiles/{Path(BG_MUSIC['path']).name}"
                          if BG_MUSIC["path"] else None),
                  "start": r.get("music_start", 0),
                  "volume": r.get("music_volume", 12)},
        "video_volume": r.get("video_volume", 100),
        "segments": r.get("segments") or [{"s": cs, "e": ce}],
        "overlays": [{**o, "url": _rel_url(o["path"], CLIPS, "/clips")}
                     for o in (r.get("overlays") or [])],
        "sub_size": r.get("sub_size", 88),
        "template": r.get("template", "sora-clean"),
        "sub_font": r.get("sub_font") or CUSTOM_FONT["family"] or "Arial Black",
        "has_audio": has_audio(r["video"]),
        "clips": [{"i": i + 1, "title": c.get("title", f"Corte {i+1}"),
                   "duration": c.get("duration"), "score": c.get("score")}
                  for i, c in enumerate(job.get("clips", []))],
    }


@app.post("/api/jobs/{job_id}/clips/{idx}/asset")
async def drop_asset(job_id: str, idx: int, file: UploadFile = File(...),
                     fit: str = Form("contain"), destino: str = Form("topo"),
                     s: float = Form(0), e: float = Form(3)):
    """Recebe imagem arrastada. `destino`='topo' troca a imagem de cima;
    'overlay' cria uma imagem sobreposta na timeline. Só guarda — o render
    sai na exportação, então a prévia atualiza na hora."""
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    ext = Path(file.filename or "img.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        return JSONResponse({"detail": f"imagem não suportada: {ext}"},
                            status_code=400)
    pasta = Path(r["mp4"]).parent
    if destino == "overlay":
        n = len(r.get("overlays") or []) + 1
        dest = pasta / f"ov_{idx}_{n}_{uuid.uuid4().hex[:4]}{ext}"
    else:
        dest = pasta / f"apoio_{idx}{ext}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 512):
            f.write(chunk)
    if destino == "overlay":
        ov = {"path": str(dest), "s": float(s), "e": float(e),
              "x": 0.5, "y": 0.28, "w": 0.5,
              "name": Path(file.filename or dest.name).name}
        r.setdefault("overlays", [])
        r["overlays"].append(ov)
        save_job(job)
        return {"ok": True, "overlay": {**ov,
                "url": _rel_url(dest, CLIPS, "/clips")}}
    r["top_image"] = str(dest)
    r["top_fit"] = "contain" if fit == "contain" else "cover"
    save_job(job)
    return {"ok": True, "url": _rel_url(dest, CLIPS, "/clips"),
            "fit": r["top_fit"]}


@app.get("/api/jobs/{job_id}/clips/{idx}/filmstrip")
def filmstrip(job_id: str, idx: int, frames: int = 30):
    """Faixa de miniaturas do trecho, como a régua de vídeo do CapCut."""
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    dur = max(r["end"] - r["start"], 0.5)
    n = max(6, min(int(frames), 60))
    dest = Path(r["mp4"]).parent / f"strip_{idx}.jpg"
    fps = n / dur
    try:
        subprocess.run(
            [FFMPEG, "-y", "-ss", str(r["start"]), "-t", f"{dur:.3f}",
             "-i", str(Path(r["video"]).resolve()),
             "-vf", f"fps={fps:.4f},scale=96:-1,tile={n}x1",
             "-frames:v", "1", "-q:v", "5", str(dest)],
            check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        return JSONResponse({"detail": (e.stderr or str(e))[-300:]},
                            status_code=500)
    return {"url": _rel_url(dest, CLIPS, "/clips"), "frames": n}


@app.get("/api/jobs/{job_id}/clips/{idx}/waveform")
def waveform(job_id: str, idx: int):
    """Forma de onda do áudio do trecho (para a faixa de áudio da timeline)."""
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    if not has_audio(r["video"]):
        return {"url": None}
    dur = max(r["end"] - r["start"], 0.5)
    dest = Path(r["mp4"]).parent / f"wave_{idx}.png"
    try:
        subprocess.run(
            [FFMPEG, "-y", "-ss", str(r["start"]), "-t", f"{dur:.3f}",
             "-i", str(Path(r["video"]).resolve()),
             "-filter_complex",
             "aformat=channel_layouts=mono,"
             "showwavespic=s=1200x60:colors=0x00e0a4",
             "-frames:v", "1", str(dest)],
            check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        return JSONResponse({"detail": (e.stderr or str(e))[-300:]},
                            status_code=500)
    return {"url": _rel_url(dest, CLIPS, "/clips")}


class EditorSave(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    lines: List[TLLine] = []            # modo antigo (timeline rápida)
    blocks: Optional[List[TLLine]] = None   # modo editor: lista completa
    music_start: float = 0
    music_volume: int = 12
    video_volume: int = 100
    headline: Optional[str] = None
    sub_size: Optional[int] = None       # tamanho da fonte da legenda
    sub_font: Optional[str] = None       # família da fonte da legenda
    template: Optional[str] = None       # modelo visual da legenda
    segments: Optional[List[dict]] = None   # trechos do vídeo (tempo da fonte)
    overlays: Optional[List[dict]] = None   # imagens sobrepostas


def rebuild_from_blocks(job, r, blocks):
    """Reconstrói TODAS as palavras do trecho a partir dos blocos do editor.
    Permite dividir, excluir, juntar e adicionar blocos livremente."""
    cs, ce = r["start"], r["end"]
    novas, grupos = [], []
    for b in sorted(blocks, key=lambda x: x.s):
        toks = (b.text or "").split()
        if not toks:
            continue
        s = cs + max(0.0, float(b.s))
        e = cs + max(float(b.s) + 0.15, float(b.e))
        e = min(e, ce)
        if e <= s:
            continue
        tot = sum(len(t) for t in toks) or 1
        cur = s
        for t in toks:
            dt = (e - s) * len(t) / tot
            novas.append({"w": t, "s": round(cur, 3), "e": round(cur + dt, 3)})
            cur += dt
        grupos.append(len(toks))
    fora = [w for w in job["_words"] if not (cs <= w["s"] < ce)]
    job["_words"] = sorted(fora + novas, key=lambda w: w["s"])
    r["groups"] = grupos


def _aplicar_estado_editor(job, r, idx, data):
    """Grava no projeto tudo que veio do editor, SEM renderizar.

    Separado do render porque renderizar leva ~30s: assim dá para salvar o
    trabalho em andamento sem esperar (e sem perder nada ao sair da tela)."""
    # manchete alterada -> regenera a tarja
    if data.headline is not None and r["layout"] == "split" and r.get("banner"):
        cl = job["clips"][idx - 1] if len(job.get("clips", [])) >= idx else None
        if cl is not None and data.headline != cl.get("headline"):
            make_banner(data.headline, Path(r["banner"]))
            cl["headline"] = data.headline
    # 1) corte do vídeo primeiro — os blocos vêm relativos ao novo início
    if (data.start is not None and data.end is not None
            and data.end > data.start):
        r["start"], r["end"] = round(data.start, 2), round(data.end, 2)
    # 2) blocos são a fonte da verdade das legendas
    if data.blocks is not None:
        rebuild_from_blocks(job, r, data.blocks)
    r["music_start"] = max(0.0, float(data.music_start))
    r["music_volume"] = max(0, min(int(data.music_volume), 100))
    r["video_volume"] = max(0, min(int(data.video_volume), 200))
    if data.sub_size is not None:
        r["sub_size"] = max(24, min(int(data.sub_size), 200))
    if data.sub_font is not None:
        r["sub_font"] = data.sub_font or None
    if data.template is not None:
        r["template"] = data.template
        t = tpl(data.template)
        if data.sub_size is None:
            r["sub_size"] = t["size"]
        if not data.sub_font:
            r["sub_font"] = t["font"]
    if data.segments is not None:
        segs = [s for s in data.segments
                if float(s.get("e", 0)) - float(s.get("s", 0)) > 0.1]
        r["segments"] = segs or None
    if data.overlays is not None:
        r["overlays"] = data.overlays or None
    if len(job.get("clips", [])) >= idx:
        cl = job["clips"][idx - 1]
        cl["start"], cl["end"] = r["start"], r["end"]
        cl["duration"] = round(r["end"] - r["start"], 1)


@app.post("/api/jobs/{job_id}/clips/{idx}/editor/state")
def editor_save_state(job_id: str, idx: int, data: EditorSave):
    """Salva o andamento da edição sem gerar o vídeo (rápido)."""
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    _aplicar_estado_editor(job, r, idx, data)
    save_job(job)
    return {"ok": True, "salvo": True}


@app.post("/api/jobs/{job_id}/clips/{idx}/editor")
def editor_save(job_id: str, idx: int, data: EditorSave):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    if data.blocks is not None:
        _aplicar_estado_editor(job, r, idx, data)
        segs = r.get("segments") or [{"s": r["start"], "e": r["end"]}]
        dur_saida = sum(float(s["e"]) - float(s["s"]) for s in segs)
        # legenda gerada direto dos blocos: preview == vídeo final
        build_subs_blocks([{"s": b.s, "e": b.e, "text": b.text}
                           for b in data.blocks],
                          Path(r["ass"]), Path(r["srt"]),
                          font=r.get("sub_font"), size=r.get("sub_size", 88),
                          headline=None, dur=dur_saida,
                          template=r.get("template"))
        resp = rerender_clip(job, r)
        if isinstance(resp, dict) and len(job.get("clips", [])) >= idx:
            cl = job["clips"][idx - 1]
            cl["start"], cl["end"] = r["start"], r["end"]
            cl["duration"] = round(r["end"] - r["start"], 1)
        save_job(job)
        return resp
    # sem blocos: mantém o caminho antigo (manchete/tarja e ajustes simples)
    if data.headline is not None and r["layout"] == "split" and r.get("banner"):
        cl = job["clips"][idx - 1] if len(job.get("clips", [])) >= idx else None
        if cl is not None and data.headline != cl.get("headline"):
            make_banner(data.headline, Path(r["banner"]))
            cl["headline"] = data.headline
    return edit_timeline(job_id, idx,
                         TimelineEdit(start=data.start, end=data.end,
                                      lines=data.lines,
                                      music_start=data.music_start,
                                      music_volume=data.music_volume,
                                      video_volume=data.video_volume))


# --------------------------------------------------------------------------
# Timeline (tempos de legenda, música e trecho do corte)
# --------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}/clips/{idx}/timeline")
def get_timeline(job_id: str, idx: int):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado (gere-o nesta "
                             "sessão do servidor)"}, status_code=404)
    cs, ce = r["start"], r["end"]
    lines = group_lines(job["_words"], cs, ce)
    total = job["_words"][-1]["e"] if job["_words"] else ce
    return {
        "start": cs, "end": ce, "duration": round(ce - cs, 3),
        "video_total": round(total, 2),
        "lines": [{"text": " ".join(w["w"] for w in ln),
                   "s": round(ln[0]["s"] - cs, 3),
                   "e": round(min(ln[-1]["e"], ce) - cs, 3)} for ln in lines],
        "music": {"name": BG_MUSIC["name"],
                  "start": r.get("music_start", 0),
                  "volume": r.get("music_volume", 12)},
    }


@app.post("/api/jobs/{job_id}/clips/{idx}/timeline")
def edit_timeline(job_id: str, idx: int, data: TimelineEdit):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    cs, ce = r["start"], r["end"]
    lines = group_lines(job["_words"], cs, ce, groups=r.get("groups"))
    if data.lines and len(data.lines) != len(lines):
        return JSONResponse({"detail": "número de linhas não confere"},
                            status_code=400)
    # remapeia as palavras de cada linha para a nova janela de tempo
    if data.lines:
        novas, grupos = [], []
        anterior_fim = cs                      # impede sobreposição entre linhas
        for ln, nt in zip(lines, data.lines):
            s_new, e_new = cs + nt.s, cs + nt.e
            if s_new < anterior_fim:           # empurra para depois da anterior
                desloc = anterior_fim - s_new
                s_new, e_new = s_new + desloc, e_new + desloc
            if e_new - s_new < 0.15:
                e_new = s_new + 0.15
            anterior_fim = e_new
            span_n = e_new - s_new
            tokens = (nt.text or "").split() if nt.text is not None else None
            if tokens is not None and tokens != [w["w"] for w in ln]:
                # texto do bloco foi editado -> distribui o tempo pelas palavras
                tot = sum(len(t) for t in tokens) or 1
                cur = s_new
                for t_ in tokens:
                    dt = span_n * len(t_) / tot
                    novas.append({"w": t_, "s": round(cur, 3),
                                  "e": round(cur + dt, 3)})
                    cur += dt
                grupos.append(len(tokens))     # bloco mantém suas palavras
                continue
            o_s, o_e = ln[0]["s"], ln[-1]["e"]
            span_o = max(o_e - o_s, 0.001)
            for w in ln:
                novas.append({
                    "w": w["w"],
                    "s": round(s_new + (w["s"] - o_s) / span_o * span_n, 3),
                    "e": round(s_new + (w["e"] - o_s) / span_o * span_n, 3)})
            grupos.append(len(ln))
        fora = [w for w in job["_words"] if not (cs <= w["s"] < ce)]
        job["_words"] = sorted(fora + novas, key=lambda w: w["s"])
        r["groups"] = grupos        # preserva a divisão exata dos blocos
    # trecho do corte (trim)
    if data.start is not None and data.end is not None and data.end > data.start:
        r["start"], r["end"] = round(data.start, 2), round(data.end, 2)
    r["music_start"] = max(0.0, float(data.music_start))
    r["music_volume"] = max(0, min(int(data.music_volume), 100))
    r["video_volume"] = max(0, min(int(data.video_volume), 200))
    build_subs(job["_words"], r["start"], r["end"], Path(r["ass"]),
               Path(r["srt"]), headline=r.get("headline"),
               groups=r.get("groups"))
    resp = rerender_clip(job, r)
    if isinstance(resp, dict) and len(job.get("clips", [])) >= idx:
        cl = job["clips"][idx - 1]        # reflete o novo trecho no card
        cl["start"], cl["end"] = r["start"], r["end"]
        cl["duration"] = round(r["end"] - r["start"], 1)
    save_job(job)
    return resp


# --------------------------------------------------------------------------
# Fonte customizada (.ttf / .otf)
# --------------------------------------------------------------------------

@app.get("/api/font")
def get_font():
    return {"family": CUSTOM_FONT["family"]}


def _family_of(p):
    try:
        from PIL import ImageFont
        return ImageFont.truetype(str(p), 40).getname()[0] or Path(p).stem
    except Exception:
        return Path(p).stem


@app.get("/api/fonts")
def list_fonts():
    """Fontes disponíveis (as enviadas + as do sistema mais usadas)."""
    envs = []
    for f in sorted(FONTS.glob("*")):
        if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
            envs.append({"family": _family_of(f), "file": f.name,
                         "url": f"/fontfiles/{f.name}"})
    return {"fonts": envs,
            "system": ["Arial Black", "Impact", "Helvetica", "Georgia",
                       "Verdana", "Trebuchet MS"]}


@app.post("/api/font")
async def upload_font(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".ttf", ".otf", ".ttc"):
        return JSONResponse({"detail": "envie um arquivo .ttf, .otf ou .ttc"},
                            status_code=400)
    nome = re.sub(r"[^\w.\- ]", "_", Path(file.filename).name)
    dest = FONTS / nome
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 512):
            f.write(chunk)
    family = _family_of(dest)
    CUSTOM_FONT["path"], CUSTOM_FONT["family"] = str(dest), family
    return {"ok": True, "family": family, "file": dest.name,
            "url": f"/fontfiles/{dest.name}"}


@app.delete("/api/font")
def clear_font():
    CUSTOM_FONT["path"], CUSTOM_FONT["family"] = None, None
    return {"ok": True}


# --------------------------------------------------------------------------
# Música de fundo
# --------------------------------------------------------------------------

@app.get("/api/music")
def get_music():
    return {"name": BG_MUSIC["name"]}


@app.post("/api/music")
async def upload_music(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"):
        return JSONResponse({"detail": "envie um áudio .mp3, .m4a, .wav, "
                             ".aac, .ogg ou .flac"}, status_code=400)
    dest = MUSIC / f"trilha{ext}"
    for old in MUSIC.glob("trilha.*"):        # remove trilha anterior
        try:
            old.unlink()
        except Exception:
            pass
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    BG_MUSIC["path"] = str(dest)
    BG_MUSIC["name"] = Path(file.filename or "trilha").name
    return {"ok": True, "name": BG_MUSIC["name"]}


@app.delete("/api/music")
def clear_music():
    BG_MUSIC["path"], BG_MUSIC["name"] = None, None
    return {"ok": True}


# --------------------------------------------------------------------------
# Imagem de apoio por corte (tela dividida)
# --------------------------------------------------------------------------

@app.post("/api/jobs/{job_id}/clips/{idx}/image")
async def upload_clip_image(job_id: str, idx: int,
                            file: UploadFile = File(...),
                            fit: str = Form("contain")):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    if r["layout"] != "split":
        return JSONResponse({"detail": "imagem de apoio só no layout Viral "
                             "(frame + tarja)"}, status_code=400)
    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    dest = Path(r["mp4"]).parent / f"apoio_{idx}{ext}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 512):
            f.write(chunk)
    r["top_image"] = str(dest)
    r["top_fit"] = "contain" if fit == "contain" else "cover"
    return rerender_clip(job, r)


@app.delete("/api/jobs/{job_id}/clips/{idx}/image")
def remove_clip_image(job_id: str, idx: int):
    job = JOBS.get(job_id)
    r = (job or {}).get("_render", {}).get(idx)
    if not r:
        return JSONResponse({"detail": "corte não encontrado"}, status_code=404)
    r["top_image"] = None
    return rerender_clip(job, r)


# --------------------------------------------------------------------------
# Publicação no YouTube (OAuth + upload via YouTube Data API v3)
# --------------------------------------------------------------------------

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YT_CLIENT_SECRET = YOUTUBE_DIR / "client_secret.json"
YT_TOKEN = YOUTUBE_DIR / "token.json"
YT_AUTH_STATE = {"rodando": False, "erro": None}
PUBLISH: dict = {}          # id -> {step, progress, detail, url}


def yt_credentials():
    """Carrega (e renova se preciso) as credenciais salvas. None se não autorizado."""
    if not YT_LIBS_OK or not YT_TOKEN.exists():
        return None
    try:
        creds = GoogleCredentials.from_authorized_user_file(str(YT_TOKEN), YT_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            YT_TOKEN.write_text(creds.to_json())
        return creds
    except Exception:
        return None


def _yt_run_auth_flow():
    YT_AUTH_STATE.update({"rodando": True, "erro": None})
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(YT_CLIENT_SECRET), YT_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True,
                                      authorization_prompt_message="",
                                      success_message="Autorizado! Pode fechar esta aba "
                                                      "e voltar para o CortesViral.")
        YT_TOKEN.write_text(creds.to_json())
    except Exception as e:
        YT_AUTH_STATE["erro"] = str(e)
    finally:
        YT_AUTH_STATE["rodando"] = False


@app.get("/api/youtube/status")
def yt_status():
    creds = yt_credentials()
    return {"libs_ok": YT_LIBS_OK, "tem_client_secret": YT_CLIENT_SECRET.exists(),
            "autorizado": bool(creds and creds.valid),
            "autorizando": YT_AUTH_STATE["rodando"], "erro": YT_AUTH_STATE["erro"]}


@app.post("/api/youtube/client_secret")
async def yt_upload_secret(file: UploadFile = File(...)):
    if not YT_LIBS_OK:
        return JSONResponse({"detail": "Dependências do YouTube não instaladas. "
                             "Rode: pip install -r requirements.txt"}, status_code=400)
    data = await file.read()
    try:
        d = json.loads(data)
        if "installed" not in d and "web" not in d:
            raise ValueError("formato inesperado")
    except Exception:
        return JSONResponse({"detail": "Arquivo não parece ser um client_secret.json "
                             "válido (baixe em APIs e Serviços > Credenciais, tipo "
                             "'Aplicativo para computador')."}, status_code=400)
    YT_CLIENT_SECRET.write_bytes(data)
    return {"ok": True}


@app.post("/api/youtube/auth")
def yt_auth():
    if not YT_LIBS_OK:
        return JSONResponse({"detail": "Dependências do YouTube não instaladas. "
                             "Rode: pip install -r requirements.txt"}, status_code=400)
    if not YT_CLIENT_SECRET.exists():
        return JSONResponse({"detail": "Envie o client_secret.json primeiro."},
                            status_code=400)
    if YT_AUTH_STATE["rodando"]:
        return {"ok": True, "detail": "autorização já em andamento — verifique o "
                                      "navegador"}
    threading.Thread(target=_yt_run_auth_flow, daemon=True).start()
    return {"ok": True}


@app.post("/api/youtube/logout")
def yt_logout():
    if YT_TOKEN.exists():
        YT_TOKEN.unlink()
    return {"ok": True}


class PublishRequest(BaseModel):
    job_id: str
    clip: int
    title: str
    description: str = ""
    tags: str = ""                    # separadas por vírgula
    privacy: str = "unlisted"         # public | unlisted | private
    is_short: bool = True


def _yt_publicar(pid, req: PublishRequest):
    st = PUBLISH[pid]
    try:
        job = JOBS.get(req.job_id)
        if not job:
            raise RuntimeError("Projeto não encontrado (ainda em memória?)")
        clips = job.get("clips") or []
        if not (1 <= req.clip <= len(clips)):
            raise RuntimeError("Corte não encontrado nesse projeto.")
        mp4_path = CLIPS / req.job_id / Path(clips[req.clip - 1]["mp4"]).name
        if not mp4_path.exists():
            raise RuntimeError("Arquivo do corte não encontrado em disco.")
        creds = yt_credentials()
        if not creds or not creds.valid:
            raise RuntimeError("Não autorizado — clique em 'Autorizar no YouTube' "
                              "antes de publicar.")
        yt = yt_build("youtube", "v3", credentials=creds)
        titulo = (req.title or "Corte viral").strip()[:100]
        desc = req.description or ""
        if req.is_short and "#shorts" not in (titulo + desc).lower():
            desc = (desc + "\n\n#Shorts").strip()
        body = {
            "snippet": {"title": titulo, "description": desc,
                       "tags": [t.strip() for t in req.tags.split(",") if t.strip()],
                       "categoryId": "22"},
            "status": {"privacyStatus": req.privacy
                       if req.privacy in ("public", "unlisted", "private")
                       else "unlisted",
                      "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(mp4_path), mimetype="video/mp4",
                                resumable=True, chunksize=4 * 1024 * 1024)
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        st["step"] = "enviando"
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                st["progress"] = int(status.progress() * 100)
        st["video_id"] = response.get("id")
        st["url"] = f"https://youtube.com/watch?v={response.get('id')}"
        st["step"] = "concluido"
        st["progress"] = 100
    except Exception as e:
        st["step"] = "erro"
        st["detail"] = str(e)


@app.post("/api/youtube/publish")
def yt_publish(req: PublishRequest):
    pid = uuid.uuid4().hex[:8]
    PUBLISH[pid] = {"step": "iniciando", "progress": 0, "detail": "",
                    "url": None, "video_id": None}
    threading.Thread(target=_yt_publicar, args=(pid, req), daemon=True).start()
    return {"id": pid}


@app.get("/api/youtube/publish/{pid}")
def yt_publish_status(pid: str):
    st = PUBLISH.get(pid)
    if not st:
        return JSONResponse({"detail": "envio não encontrado"}, status_code=404)
    return st


@app.get("/")
def index():
    # sem cache: garante que a interface mais recente sempre carregue
    return FileResponse(RES / "static" / "index.html",
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/editor")
def editor_page():
    return FileResponse(RES / "static" / "editor.html",
                        headers={"Cache-Control": "no-store, max-age=0"})


app.mount("/clips", StaticFiles(directory=CLIPS), name="clips")
app.mount("/work", StaticFiles(directory=WORK), name="work")        # vídeo fonte
app.mount("/musicfiles", StaticFiles(directory=MUSIC), name="musicfiles")
app.mount("/fontfiles", StaticFiles(directory=FONTS), name="fontfiles")
app.mount("/", StaticFiles(directory=RES / "static", html=True), name="static")


# --------------------------------------------------------------------------
# Execução direta (duplo clique no .exe) — sobe o servidor e abre o navegador
# --------------------------------------------------------------------------

def main():
    import webbrowser
    import socket
    import uvicorn

    porta = 8000
    # se a 8000 estiver ocupada, procura a próxima livre
    for tentativa in range(8000, 8020):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", tentativa)) != 0:
                porta = tentativa
                break

    url = f"http://localhost:{porta}"
    print("=" * 58)
    print("  CortesViral — cortes virais com legenda automática")
    print("=" * 58)
    print(f"  ffmpeg: {FFMPEG}")
    print(f"  Seus arquivos ficam em: {BASE}")
    print(f"  Abrindo {url}")
    print("  (deixe esta janela aberta enquanto usa o programa)")
    print("=" * 58)
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=porta, log_level="warning")


if __name__ == "__main__":
    main()
