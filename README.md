# 🎙️ Chicco — Local AI Voice Agent

Assistente vocale **100% locale e offline** — nato su Windows, ora anche su macOS
e Linux (vedi [Compatibilità](#-compatibilità-multipiattaforma)): parli col microfono, capisce,
esegue comandi reali sul PC (cartelle, file, app, siti, volume, ricerche web) e ti
risponde a voce. Nessuna API key, nessun cloud, nessun costo per token.

Il progetto nasce come dimostrazione di [Laya](https://pypi.org/project/laya/), un
decision-engine non autoregressivo usato qui per la classificazione degli intent.

![stack](https://img.shields.io/badge/stack-Python%203.10%2B-blue) ![license](https://img.shields.io/badge/license-private-lightgrey) ![STT](https://img.shields.io/badge/STT-faster--whisper%20%7C%20CTranslate2-purple)

---

## 🧩 Cosa fa

| Comando a voce/testo | Risultato |
|---|---|
| *"Crea una cartella chiamata Prova sul desktop"* | cartella reale sul disco |
| *"Elimina la cartella Prova"* | nel cestino (recuperabile) |
| *"Crea un file di testo chiamata spesa con dentro latte e pane"* | file .txt con contenuto |
| *"Aggiungi al file spesa la riga uova"* / *"Leggi il file spesa"* | append / lettura a voce |
| *"Appunta che domani ho la dentista alle 15"* | nota in `Note.txt` sul desktop |
| *"Apri Steam" / "Apri calcolatrice" / "Apri EarTrumpet"* | qualsiasi app: menu Start, Microsoft Store/AppX, eseguibili portabili, PATH |
| *"Apri youtube" / "Vai su gmail"* | browser **predefinito** (ShellExecute) |
| *"Cerca gatti buffi su youtube"* | ricerca diretta su YouTube/Google |
| *"Quali giochi ho su steam?"* / *"Quali app ho installato?"* | elenco parlato + **modale a schermo** con la lista completa |
| *"Che ore sono? / Che giorno è oggi?"* | ora e data a voce |
| *"Alza il volume / Muto"* | controllo audio reale (pycaw) |
| *"Metti il volume al 30" / "al settanta" / "a metà" / "del 20"* | livello assoluto (cifre, parole o %) o relativo, con verifica del valore ottenuto |
| *"Elenca i file sul desktop"* | lettura cartella reale |

All'avvio il server indicizza in una **libreria** tutto il PC (collegamenti menu
Start e desktop, app Microsoft Store/AppX, eseguibili portabili senza registro) e
legge le **librerie native dei launcher di gioco**: manifest `.acf` di Steam,
`.item` di Epic, `.info` di GOG, più i launcher Riot/EA/Ubisoft/Battle.net.
La libreria dà contesto all'IA (per aprire l'app giusta) e alimenta il comando
"quali giochi/app ho".

Il widget desktop: cerchio flottante trasparente e trascinabile, pillola di input che
appare al passaggio del mouse, toggle mute del TTS, risposte in una bolla a scomparsa.

**Correzione automatica degli input**: ogni comando — detto *o scritto* — passa da
Qwen prima dell'esecuzione, che ripulisce parole sentite/digitate male e nomi d'app
storti (`apri spotrifyt` → `apri spotify`). Le correzioni che farebbero perdere un
intent, un luogo (`desktop`, `documenti`…) o un sito noto vengono scartate; se il
nome dell'app resta irrecuperabile, un fallback fuzzy trova l'app più vicina nella
libreria. Quando una correzione viene applicata, nella bolla del widget e nella UI
compare la trascrizione originale in piccolo (🎧 "…").

**Conferma vocale**: se Qwen riscrive la trascrizione in modo radicalmente diverso
(similarità sotto soglia), Chicco non esegue nulla e chiede *'Hai detto …? Rispondi
sì o no'* — un sì vocale (anche storto: 'confirmo' vale) esegue il comando proposto,
un no annulla; dopo 90 secondi la richiesta scade e il comando successivo parte
normale.

---

## 🏗️ Architettura / Stack

```
 ┌────────────────┐  WAV    ┌──────────────────────────┐
 │ Widget Tkinter  │ ──────▶ │  Server FastAPI           │
 │ (chicco_agent/  │         │  :8123                    │
 │  widget.py)     │ ◀────── │                           │
 └────────────────┘  testo  │ 1. STT: Whisper           │
      ▲   bolla             │    large-v3-turbo         │
      │   TTS               │    (faster-whisper,       │
      │                     │    CTranslate2: CUDA o    │
 ┌──────────────┐           │    CPU int8, tutti gli OS)│
 │ UI web       │           │    fallback: Vosk it      │
 │ (browser)    │           │ 2. Correzione STT:        │
 └──────────────┘           │    Qwen ripulisce la      │
                            │    trascrizione           │
                            │    (guardie anti-danno)   │
                            │ 3. Intent: regole +       │
                            │    Laya (3 livelli)       │
                            │ 4. Comandi→JSON:          │
                            │    Qwen2.5 1.5b           │
                            │    (Ollama, locale)       │
                            │ 5. Esecuzione reale       │
                            │    sul PC + TTS           │
                            │    (pyttsx3/SAPI)         │
                            └──────────────────────────┘
```

| Livello | Tecnologia | Ruolo |
|---|---|---|
| **STT** | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) + `large-v3-turbo` CT2 (~1,6 GB): **CUDA** se hai NVIDIA, altrimenti **CPU int8** multi-thread — stesso motore su Windows, macOS e Linux. Benchmark su Ryzen 7800X3D: CPU int8 ~4 s per un comando vocale tipico (~2 s di audio). Fallback Vosk piccolo | trascrizione it/qualunque lingua (env `WHISPER_LANG=auto` per il rilevamento automatico); fallback Vosk |
| **Intent** | regole testuali + [Laya](https://pypi.org/project/laya/) (ModernBERT, probabilità calibrate) | classificare il comando in ~20 ms, 3 livelli di fallback |
| **LLM** | Qwen2.5 via [Ollama](https://ollama.com), dimensione **selezionabile** (0.5b / 1.5b / 3b — default 1.5b) | correzione della trascrizione (con guardie anti-danno: intent, luoghi, siti noti) + traduzione frasi libere in specifica JSON (`create_file{name,content}`…) + suggerimento 'Intendavi X?' per le app; fallback fuzzy `difflib` sui nomi d'app. Benchmark su Ryzen 7800X3D: 0.5b ~0,05 s/comando ma pasticcia le frasi corrette; 1.5b ~0,55 s e non tocca nulla di giusto; 3b uguale al 1.5b col doppio della RAM |
| **Esecuzione** | Python (os, subprocess, send2trash, pycaw, webbrowser) | azioni reali: file system, app, siti, volume |
| **Librerie app/giochi** | `appindex.py` + `games.py`: menu Start, Store/AppX, portabili, manifest Steam/Epic/GOG | contesto per l'IA, avvio app, elenchi su richiesta |
| **TTS** | pyttsx3 → voci SAPI di Windows (Elsa IT) | risposta vocale offline, interrotta su nuovo input |
| **GUI** | Tkinter stdlib + Pillow (icone anti-aliasate, keying trasparenza) | widget sempre-on-top trascinabile, zero dipendenze GUI |

---

## ⚙️ Requisiti

- **Windows 10/11** (esperienza completa) oppure **macOS 13+** / **Linux** (vedi Compatibilità)
- **Python 3.10+** con pip
- **GPU**: opzionale — con NVIDIA (CUDA) la trascrizione vola; su CPU pura int8 resta utilizzabile
- ~3 GB di disco per i modelli

## 🌍 Compatibilità multipiattaforma

Tutte le differenze di sistema operativo sono incapsulate in `chicco_agent/platform_utils.py`
(cartelle dati, TTS, avvio file, volume, flag subprocess): il resto del codice non fa mai
branch su `sys.platform` direttamente. La CI verifica installazione, compilazione e
scansione indici su runner Windows, macOS e Linux a ogni push.

| Funzione | Windows | macOS | Linux |
|---|---|---|---|
| Server, pipeline, intent, correzione STT, UI web | ✅ | ✅ | ✅ |
| STT Whisper (faster-whisper) | ✅ CUDA/CPU | ✅ CUDA/**Metal**/CPU | ✅ CUDA/CPU |
| TTS italiano | ✅ SAPI (Elsa) | ✅ NSSpeech (Alice) | ✅ espeak-ng (`apt install espeak-ng`) |
| Indice app | menu Start, Store/AppX, portabili | `/Applications` | `.desktop` (XDG) |
| Librerie giochi | Steam, Epic, GOG, launcher | **Steam** (stesso formato `.acf`) | **Steam** (stesso formato) |
| Widget | trasparente click-through | semi-trasparente (Aqua) | semi-trasparente |
| Volume | pycaw | osascript | pactl (se presente) |
| Cartelle dati | `%LOCALAPPDATA%\chicco` | `~/Library/Application Support/chicco` | `~/.local/share/chicco` |
| Installazione Ollama (`chicco setup`) | winget | brew | script ufficiale |

Nota: la repo Systran ufficiale del modello è risultata inaccessibile, quindi si usa
la conversione CT2 di riferimento della community (`deepdml/faster-whisper-large-v3-turbo-ct2`).
Con GPU NVIDIA aggiungi i CUDA cuDNN (vedi sotto) per l'accelerazione; altrimenti CPU int8.

## 📦 Installazione — un solo comando

`chicco run` fa **tutto in automatico**: installa le dipendenze mancanti, Ollama
(via winget su Windows, brew su macOS, script ufficiale su Linux), i modelli Qwen
(1.5b predefinito + 0.5b di riserva), Whisper large-v3-turbo CTranslate2 (~1,6 GB,
tutti gli OS) e Vosk di fallback, poi avvia server e widget.

### Windows

```bat
pip install git+https://github.com/giovannimazza/chicco-local-ai-agent.git
chicco run
```

### macOS

```bash
pip3 install git+https://github.com/giovannimazza/chicco-local-ai-agent.git
chicco run
```

Prima volta su macOS: se `brew` manca installalo da [brew.sh](https://brew.sh),
poi serve la concessione microfono quando macOS la chiede al primo avvio.

### Linux

```bash
sudo apt install python3-pip espeak-ng libportaudio2
pip3 install git+https://github.com/giovannimazza/chicco-local-ai-agent.git
chicco run
```

`espeak-ng` fornisce la voce TTS, `libportaudio2` il microfono; su distro non-Debian
usa l'equivalente del tuo gestore pacchetti.

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
pip install fastapi uvicorn laya pyttsx3 vosk soundcard numpy pillow send2trash faster-whisper pycaw comtypes

:: 2. Ollama + modelli (1.5b e' il default, 0.5b la riserva)
winget install Ollama.Ollama
ollama pull qwen2.5:1.5b
ollama pull qwen2.5:0.5b

:: 3. Whisper large-v3-turbo CTranslate2 (~1,6 GB, tutti gli OS)
:: scaricato automaticamente anche da `chicco setup`/`chicco run`
huggingface-cli download deepdml/faster-whisper-large-v3-turbo-ct2 ^
  --local-dir %USERPROFILE%\.cache\whisper\faster-whisper-large-v3-turbo
```

> Vosk (fallback STT) scarica il modello `vosk-model-small-it-0.22` in
> `~/.cache/vosk/` al primo avvio se assente; senza il modello CT2 di Whisper si
> usa solo Vosk. Con GPU NVIDIA installa i CUDA cuDNN per l'accelerazione:
> `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*`

## 🚀 Avvio manuale (senza CLI)

```bat
:: terminale 1 — server (o lascia che lo avvii il widget da solo)
python chicco_agent\server.py

:: terminale 2 — widget desktop (niente console con pythonw)
pythonw chicco_agent\widget.py

:: alternativa: UI nel browser su http://127.0.0.1:8123
```

> I vecchi script `voice_assistant_server.py` e `assistant_widget.py` alla radice
> sono shim retrocompatibili che puntano al pacchetto. Su macOS/Linux: `python3
> chicco_agent/server.py` e `python3 chicco_agent/widget.py`.
>
> File runtime (cache indici, wav, posizioni): `%LOCALAPPDATA%\chicco` su Windows,
> `~/Library/Application Support/chicco` su macOS, `~/.local/share/chicco` su Linux.

### Usare il widget
- **Click** sul cerchio → registra; **secondo click** → invia
- **Doppio click** → info sul trascrittore attivo (Whisper/Vosk, modello, dispositivo)
- **Mouse over** → appare pillola di input (Invio = manda) e **toggle mute TTS** 🔊/🔇
- **Trascina** il widget dove vuoi (la posizione si ricorda)
- **Click destro** → chiudi

### UI web
Interfaccia chat stile ChatGPT su `http://127.0.0.1:8123`: messaggi con avatar,
hero con comandi suggeriti, microfono nel composer. Le risposte che contengono
elenchi (giochi, app) aprono una **modale** con la lista completa; il pulsante
**🗂️ App e giochi** (fisso in alto a destra) apre la libreria completa con
categorie **comprimibili** (Giochi / Applicazioni / Strumenti di sistema) e
**ricerca per nome**.

### API
```bash
curl -X POST http://127.0.0.1:8123/api/text -H "Content-Type: application/json" ^
     -d '{"text":"crea una cartella chiamata Prova sul desktop"}'
```

| Endpoint | Uso |
|---|---|
| `POST /api/text` | esegue un comando testuale |
| `POST /api/listen` | audio webm dal browser (via ffmpeg) |
| `POST /api/listen_wav` | WAV PCM dal widget |
| `GET /api/apps[?q=termine]` | libreria app + giochi, con categorie (ricerca opzionale) |
| `POST /api/apps/rescan` | reindicizza le app |
| `GET /api/list` | ultima lista giochi/app richiesta a voce |
| `GET /api/stt` | trascrittore attivo (motore, modello, dispositivo) |
| `POST /api/normalize` | corregge una trascrizione con Qwen senza eseguirla: `{raw, text, corrected}` |
| `GET/POST /api/model` | modello LLM attivo / cambia modello (persistito, menu nel widget e nella UI) |
| `GET /_tts_reply.wav` | ultima risposta vocale |

## 📁 Struttura del progetto

```
chicco_agent/
├── server.py         # FastAPI: STT (faster-whisper/Vosk), intent, LLM, esecuzione, TTS, API
├── widget.py         # widget desktop Tkinter (trasparente, trascinabile)
├── ui.html           # UI web stile ChatGPT
├── cli.py            # comandi chicco run/setup/doctor/stop
├── platform_utils.py # astrazioni OS: cartelle, TTS, open, volume, subprocess
├── appindex.py       # libreria app: lnk/Store/portabili, /Applications, .desktop
└── games.py          # librerie giochi: Steam (win/mac/linux), Epic, GOG
```

File runtime (cache indici, wav, posizioni) in `%LOCALAPPDATA%\chicco` (Windows),
`~/Library/Application Support/chicco` (macOS) o `~/.local/share/chicco` (Linux).

## 🍎 Note per piattaforma

- **Windows**: esperienza completa — Whisper via faster-whisper (CUDA o CPU), widget
  trasparente click-through, TTS SAPI con voci italiane, controllo volume pycaw.
- **macOS**: **Whisper ora funziona anche qui**: faster-whisper gira su Metal
  (configurabile) o CPU, TTS con la voce di sistema, widget semi-trasparente (Aqua non
  supporta il keying a colore), giochi letti dai manifest `.acf` di Steam
  (`~/Library/Application Support/Steam`). Al primo avvio concedere il microfono
  in Impostazioni → Privacy e sicurezza.
- **Linux**: come macOS per STT/TTS; widget con trasparenza parziale, giochi via
  Steam (`~/.steam`), indice app dai file `.desktop` XDG. Su Wayland il
  always-on-top del widget può dipendere dal compositor.
- **Ovunque**: server, pipeline (intent + correzione + conferma vocale), UI web e
  tutti gli endpoint sono identici — cambia solo la "pelle" di sistema.
- **Accelerazione Whisper**: `WHISPER_DEVICE=auto|cuda|cpu` (default: CUDA se
  presente), `WHISPER_COMPUTE=default|int8|...`, `WHISPER_LANG=it|auto`.
  Su macOS è possibile Metal via `pip install ctranslate2` con supporto Metal
  (sperimentale) o `WHISPER_DEVICE=cpu`.

## 🛠️ Estendere

- **Nuove app/siti**: dizionari `APP_ALIAS` / `SITE_ALIAS` in `chicco_agent/server.py`
- **Nuovi comandi**: aggiungi parole chiave in `KEYWORDS` + un ramo in `run_command()`
- **Altri launcher di gioco**: aggiungi un collector in `chicco_agent/games.py`
- **Disattivare Whisper**: variabile d'ambiente `WHISPER=0` (usa solo Vosk)
- **Dispositivo/compute Whisper**: env `WHISPER_DEVICE`, `WHISPER_COMPUTE`, `WHISPER_LANG` (vedi Note per piattaforma)
- **Microfono/latenza**: il modello Whisper resta residente in RAM/VRAM; avvio a freddo ~1 s

## 📄 Licenza

Uso personale. Modelli: Whisper (MIT/OpenAI), Qwen2.5 (Apache 2.0), Laya (Apache 2.0).
