"""Teste do pipeline sem rede: heurística + legendas + renderização ffmpeg."""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from app import analyze_heuristic, build_subs, render_clip, CLIPS

# 1. Transcrição simulada (fala viral em PT-BR com timestamps por palavra)
texto = ("Você sabia que 90 por cento das pessoas cometem esse erro com dinheiro? "
         "Eu descobri um segredo que mudou completamente a minha vida financeira. "
         "Preste atenção porque ninguém te conta isso. "
         "O maior erro é guardar dinheiro parado na poupança. "
         "A verdade é que a inflação come o seu dinheiro todos os dias. "
         "O que os ricos fazem é diferente: eles investem em ativos que geram renda. "
         "Comenta aqui embaixo se você já sabia disso e compartilha com um amigo.")
words, t = [], 0.5
for w in texto.split():
    d = 0.28 + 0.03 * len(w)
    words.append({"w": w, "s": round(t, 2), "e": round(t + d, 2)})
    t += d + (0.7 if w.endswith(('.', '?', '!')) else 0.06)
transcript = {"words": words, "text": texto}
total = words[-1]["e"]
print(f"[1] transcrição simulada: {len(words)} palavras, {total:.1f}s")

# 2. Análise heurística
job = {"num_clips": 2, "min_len": 10, "max_len": 30}
clips = analyze_heuristic(job, transcript)
assert clips, "heurística não retornou cortes"
for c in clips:
    print(f"[2] corte: {c['start']}-{c['end']}s score={c['score']} hook={c['hook'][:50]}...")

# 3. Vídeo sintético 16:9 1080p (barras coloridas + tom) cobrindo a duração
src = Path("/tmp/fonte.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={total+2:.0f}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={total+2:.0f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-c:a", "aac",
                str(src)], check=True, capture_output=True)
print("[3] vídeo sintético 1920x1080 gerado")

# 4. Legendas + renderização do primeiro corte
c = clips[0]
out = CLIPS / "teste"
out.mkdir(parents=True, exist_ok=True)
ass, srt, mp4 = out / "corte_1.ass", out / "corte_1.srt", out / "corte_1.mp4"
build_subs(words, c["start"], c["end"], ass, srt)
print(f"[4] legendas geradas: {len(srt.read_text().strip().split(chr(10)+chr(10)))} blocos srt")
render_clip(src, c["start"], c["end"], ass, mp4)

# 5. Verificação do output
r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,codec_name",
                    "-of", "json", str(mp4)], capture_output=True, text=True)
info = json.loads(r.stdout)["streams"][0]
assert (info["width"], info["height"]) == (1080, 1920), f"resolução errada: {info}"
print(f"[5] OK: {mp4.name} -> {info['codec_name']} {info['width']}x{info['height']}, "
      f"{mp4.stat().st_size//1024} KB")
print("PIPELINE VALIDADO")
