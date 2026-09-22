# 🎙️ Chicco — Local AI Voice Agent

Assistente vocale **100% locale e offline** per Windows: parli col microfono, capisce,
esegue comandi reali sul PC (cartelle, file, app, siti, volume, ricerche web) e ti
risponde a voce. Nessuna API key, nessun cloud, nessun costo per token.

Il progetto nasce come dimostrazione di [Laya](https://pypi.org/project/laya/), un
decision-engine non autoregressivo usato qui per la classificazione degli intent.

![stack](https://img.shields.io/badge/stack-Python%203.10%2B-blue) ![license](https://img.shields.io/badge/license-private-lightgrey) ![GPU](https://img.shields.io/badge/GPU-Vulkan%20 AMD-green)

---

## 🧩 Cosa fa

| Comando a voce/testo | Risultato |
|---|---|
| *"Crea una cartella chiamata Prova sul desktop"* | cartella reale sul disco |
| *"Elimina la cartella Prova"* | nel cestino (recuperabile) |
| *"Crea un file di testo chiamata spesa con dentro latte e pane"* | file .txt con contenuto |
| *"Aggiungi al file spesa la riga uova"* / *"Leggi il file spesa"* | append / lettura a voce |
| *"Appunta che domani ho la dentista alle 15"* | nota in `Note.txt` sul desktop |
| *"Apri Steam" / "Apri calcolatrice"* | qualsiasi app installata (menu Start + PATH) |
| *"Apri youtube" / "Vai su gmail"* | browser **predefinito** (ShellExecute) |
| *"Cerca gatti buffi su youtube"* | ricerca diretta su YouTube/Google |
| *"Che ore sono? / Che giorno è oggi?"* | ora e data a voce |
| *"Alza il volume / Muto"* | controllo audio reale (pycaw) |
| *"Elenca i file sul desktop"* | lettura cartella reale |

Il widget desktop: cerchio flottante trasparente e trascinabile, pillola di input che
appare al passaggio del mouse, toggle mute del TTS, risposte in una bolla a scomparsa.

---

## 🏗️ Architettura / Stack

```
 ┌──────────────┐   WAV    ┌──────────────────────┐
 │ Widget Tkinter│ ───────▶ │  Server FastAPI      │
 │ (assistant_   │          │  :8123               │
 │  widget.py)   │ ◀─────── │                      │
 └──────────────┘  testo   │ 1. STT: Whisper      │
      ▲   bolla             │    large-v3-turbo    │
      │   TTS               │    GGUF Q8_0         │
      │                     │    (transcribe.cpp,  │
 ┌──────────────┐          │     Vulkan → AMD GPU)│
 │ UI web       │          │    fallback: Vosk it │
 │ (browser)    │          │ 2. Intent: regole +  │
 └──────────────┘          │    Laya (3 livelli)  │
                           │ 3. Comandi→JSON:     │
                           │    Qwen2.5 0.5B      │
                           │    (Ollama, locale)  │
                           │ 4. Esecuzione reale  │
                           │    sul PC + TTS      │
                           │    (pyttsx3/SAPI)    │
                           └──────────────────────┘
```

| Livello | Tecnologia | Ruolo |
|---|---|---|
| **STT** | [transcribe.cpp](https://github.com/handy-computer/transcribe.cpp) + `whisper-large-v3-turbo-Q8_0.gguf` su **Vulkan** (testato su AMD RX 9070 XT, ~6× realtime) | trascrizione it/qualunque lingua; fallback Vosk piccolo |
| **Intent** | regole testuali + [Laya](https://pypi.org/project/laya/) (ModernBERT, probabilità calibrate) | classificare il comando in ~20 ms, 3 livelli di fallback |
| **LLM** | Qwen2.5 0.5B via [Ollama](https://ollama.com) | tradurre frasi libere in specifica JSON (`create_file{name,content}`…) |
| **Esecuzione** | Python (os, subprocess, send2trash, pycaw, webbrowser) | azioni reali: file system, app, siti, volume |
| **TTS** | pyttsx3 → voci SAPI di Windows (Elsa IT) | risposta vocale offline, interrotta su nuovo input |
| **GUI** | Tkinter stdlib + Pillow (icone anti-aliasate, keying trasparenza) | widget sempre-on-top trascinabile, zero dipendenze GUI |

---

## ⚙️ Requisiti

- **Windows 10/11**
- **Python 3.10+** con pip
- **GPU**: opzionale ma consigliata (Vulkan per Whisper); funziona anche solo CPU
- ~3 GB di disco per i modelli

## 📦 Installazione — un solo comando

```bat
pip install git+https://github.com/giovannimazza/chicco-local-ai-agent.git
chicco run
```

`chicco run` fa **tutto in automatico**: installa le dipendenze mancanti, Ollama
(via winget), il modello Qwen2.5 0.5B, Whisper large-v3-turbo Q8_0 (~874 MB) e
Vosk di fallback, poi avvia server e widget.

Comandi disponibili:

| Comando | Effetto |
|---|---|
| `chicco run` | avvia tutto (installa prima ciò che manca) |
| `chicco run server` | solo il server, senza widget |
| `chicco setup` | solo installazione, senza avviare |
| `chicco doctor` | diagnostica: cosa è installato e cosa manca |
| `chicco stop` | ferma widget e server |

### Installazione manuale (alternativa)

```bat
:: 1. dipendenze Python
pip install fastapi uvicorn laya pyttsx3 vosk soundcard numpy pillow send2trash pycaw comtypes transcribe_cpp

:: 2. Ollama + modello per i comandi in linguaggio libero
winget install Ollama.Ollama
ollama pull qwen2.5:0.5b

:: 3. Whisper large-v3-turbo GGUF (~874 MB)
curl -L -o %USERPROFILE%\.cache\whisper\whisper-large-v3-turbo-Q8_0.gguf ^
  https://huggingface.co/handy-computer/whisper-large-v3-turbo-gguf/resolve/main/whisper-large-v3-turbo-Q8_0.gguf
```

> Vosk (fallback STT) scarica il modello `vosk-model-small-it-0.22` in
> `~/.cache/vosk/` al primo avvio se assente; senza Whisper si usa solo Vosk.

## 🚀 Avvio manuale (senza CLI)

```bat
:: terminale 1 — server (o lascia che lo avvii il widget da solo)
python voice_assistant_server.py

:: terminale 2 — widget desktop (niente console con pythonw)
pythonw assistant_widget.py

:: alternativa: UI nel browser su http://127.0.0.1:8123
```

### Usare il widget
- **Click** sul cerchio → registra; ** secondo click** → invia
- **Mouse over** → appare pillola di input (Invio = manda) e **toggle mute TTS** 🔊/🔇
- **Trascina** il widget dove vuoi (la posizione si ricorda)
- **Click destro** → chiudi

### API rapide
```bash
curl -X POST http://127.0.0.1:8123/api/text -H "Content-Type: application/json" ^
     -d '{"text":"crea una cartella chiamata Prova sul desktop"}'
```
`POST /api/listen` (webm dal browser) · `POST /api/listen_wav` (WAV dal widget) ·
`GET /api/history`.

## 🛠️ Estendere

- **Nuove app/siti**: dizionari `APP_ALIAS` / `SITE_ALIAS` in `voice_assistant_server.py`
- **Nuovi comandi**: aggiungi parole chiave in `KEYWORDS` + un ramo in `run_command()`
- **Disattivare Whisper**: variabile d'ambiente `WHISPER=0` (usa solo Vosk)
- **Microfono/latenza**: il modello Whisper resta residente in RAM/VRAM; avvio a freddo ~1 s

## 📄 Licenza

Uso personale. Modelli: Whisper (MIT/OpenAI), Qwen2.5 (Apache 2.0), Laya (Apache 2.0).
