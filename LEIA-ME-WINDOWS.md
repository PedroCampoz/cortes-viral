# CortesViral no Windows — passo a passo

## O que fazer (resumo)

1. Descompacte esta pasta em algum lugar do PC (ex.: `C:\CortesViral`)
2. Clique 2x em **`Gerar-Executavel.bat`** e espere (10 a 25 minutos, só na primeira vez)
3. No final abre a pasta `dist\CortesViral` — dentro dela, **`CortesViral.exe`** é o programa
4. Clique 2x no `CortesViral.exe` sempre que quiser usar

Pronto. A partir daí você nunca mais precisa mexer em Python nem terminal.

---

## Antes de começar: instalar o Python (uma vez)

O `Gerar-Executavel.bat` precisa do Python para montar o executável.

1. Baixe em https://www.python.org/downloads/
2. **Na primeira tela do instalador, marque a caixinha "Add Python to PATH"** — se esquecer disso, o script não vai encontrar o Python
3. Clique em "Install Now"

Depois de instalado, pode rodar o `Gerar-Executavel.bat`.

---

## O que o script faz sozinho

- Instala todas as bibliotecas necessárias
- Baixa o **ffmpeg** e embute dentro do executável (você não precisa instalar nada à parte)
- Empacota tudo num `.exe`

---

## Como usar depois de pronto

Clique 2x em `CortesViral.exe`. Vai abrir:

- uma **janela preta** (é o motor do programa — pode minimizar, mas não feche)
- o **navegador** em `http://localhost:8000`, que é onde você trabalha

Para fechar, feche a janela preta.

## Onde ficam os arquivos

Ao lado do `CortesViral.exe` aparece uma pasta **`CortesViral-dados`**:

```
CortesViral-dados\
├── clips\        <- SEUS CORTES PRONTOS ficam aqui
├── workspace\    <- vídeos baixados e transcrições (pode limpar quando quiser)
├── fonts\        <- fontes (Sora já vem instalada)
├── music\        <- trilhas enviadas pela interface
├── youtube\      <- autorização de publicação
└── config.json   <- sua chave de API
```

Você pode copiar a pasta `CortesViral` inteira para outro PC, pen drive ou onde quiser — ela é independente. Se copiar junto a pasta `CortesViral-dados`, leva também as configurações e os cortes.

---

## Primeira vez usando

Ao abrir o programa pela primeira vez:

1. **Chave de API** (topo da tela): cole sua chave do Gemini e clique em Salvar. Se preferir não usar API, escolha "Ollama" em Motor de IA (precisa do Ollama instalado) ou "Heurística" (funciona sem nada, mas escolhe os trechos de forma mais simples).
2. **Publicar no YouTube** (opcional): siga o passo a passo do `README.md` na seção correspondente.

Na primeira transcrição o programa baixa o modelo do Whisper (~500 MB) automaticamente. Isso acontece uma vez só.

---

## Problemas comuns

**"Python nao encontrado"**
Você instalou o Python sem marcar "Add Python to PATH". Reinstale marcando a caixinha, ou instale pela Microsoft Store (que já configura o PATH sozinho).

**O Windows Defender / SmartScreen bloqueia o .exe**
Normal para executáveis sem assinatura digital paga. Clique em "Mais informações" → "Executar assim mesmo". O arquivo foi gerado no seu próprio PC, a partir do código que está nesta pasta.

**O antivírus apaga o .exe**
Alguns antivírus marcam executáveis feitos com PyInstaller por engano. Adicione a pasta `dist\CortesViral` à lista de exceções do antivírus.

**O build falhou / não gerou o .exe**
Use o **`Iniciar-CortesViral.bat`** — ele roda o programa direto, sem empacotar. Funciona igual, só precisa da janela preta aberta.

**"Seu ffmpeg não suporta legendas embutidas"**
O ffmpeg que está sendo usado veio sem libass. Instale a versão completa com `winget install ffmpeg` no PowerShell e rode o programa de novo.

**A porta 8000 está ocupada**
O programa procura sozinho a próxima porta livre (8001, 8002...) e abre o navegador no endereço certo. Se o navegador não abrir, olhe o endereço que aparece na janela preta.

---

## Se tiver GPU NVIDIA

A transcrição fica bem mais rápida. O programa detecta a GPU automaticamente — não precisa configurar nada.

---

## Para atualizar o programa depois

Se você receber uma versão nova do `app.py` ou da pasta `static`, substitua os arquivos e rode o `Gerar-Executavel.bat` de novo. Seus cortes e configurações em `CortesViral-dados` não são afetados.
