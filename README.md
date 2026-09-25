# 🎙️ Ugo — Local AI Voice Agent

A **100% local and offline** voice assistant — born on Windows, now also on macOS
and Linux (see [Compatibility](#-cross-platform-compatibility)): you talk into the
microphone, it understands, executes real commands on your PC (folders, files, apps,
websites, volume, web searches) and answers you by voice. No API keys, no cloud,
no per-token cost.

The project started as a demo of [Laya](https://pypi.org/project/laya/), a
non-autoregressive decision engine used here for intent classification.

![CI](https://github.com/giovannimazza/ugo-local-ai-agent/actions/workflows/ci.yml/badge.svg) ![stack](https://img.shields.io/badge/stack-Python%203.10%2B-blue) ![license](https://img.shields.io/badge/license-private-lightgrey) ![STT](https://img.shields.io/badge/STT-faster--whisper%20%7C%20CTranslate2-purple)

---

## 🌐 Language

The web UI has a **🌐 flag button** to switch between **Italiano** and **English**.
The choice is global and persisted: it translates the web interface and the desktop
widget (in sync), switches the Whisper transcription language and pairs the default
Piper voice — **Italian → Paola**, **English → Amy** (Amy is auto-downloaded, ~63 MB,
on first switch). English commands are translated into their Italian equivalents by
Qwen before execution, and assistant replies are translated back into English, so the
command engine stays uniform.

## 🧩 What it does

| Voice/text command | Result |
|---|---|
| *"Crea una cartella chiamata Prova sul desktop"* (Create a folder named Prova on the desktop) | real folder on disk |
| *"Elimina la cartella Prova"* (Delete the Prova folder) | moved to trash (recoverable) |
| *"Crea un file di testo chiamata spesa con dentro latte e pane"* (Create a text file called groceries with milk and bread in it) | .txt file with content |
| *"Aggiungi al file spesa la riga uova"* / *"Leggi il file spesa"* (Add eggs to the grocery file / Read the grocery file) | append / spoken read-back |
| *"Appunta che domani ho la dentista alle 15"* (Note that I have the dentist tomorrow at 3) | note in `Note.txt` on the desktop |
| *"Apri Steam"* / *"Apri calcolatrice"* (Open Steam / Open Calculator) | any app: Start menu, Microsoft Store/AppX, portable executables, PATH |
| *"Chiudi Spotify"* / *"chiudi il blocco note forza"* (Close Spotify / force-close Notepad) | closes the app if running (graceful → forced taskkill, with process verification); refuses system processes |
| *"Apri youtube"* / *"Vai su gmail"* (Open YouTube / Go to Gmail) | **default** browser (ShellExecute) |
| *"Apri youtube e discord"* (Open YouTube and Discord) | **multi-command**: both actions run in sequence with one spoken summary — also *"muto e apri spotify"*, *"metti il volume al 30 e apri steam"*, *"apri youtube, discord e steam"* (list form). Conservative splitter: file/search commands never split ("create a file called groceries and bread" stays one command) |
| *"Cerca gatti buffi su youtube"* (Search funny cats on YouTube) | direct YouTube/Google search |
| *"Quali giochi ho su steam?"* / *"Quali app ho installato?"* (What games do I have on Steam? / What apps are installed?) | spoken list + **on-screen modal** with the full list |
| *"Che ore sono?"* (What time is it?) | time and date by voice |
| *"Quanto fa 1+1?"* / *"Chi ha inventato il telefono?"* (What is 1 plus 1? / Who invented the telephone?) | **agent-style chat fallback**: what is not a command goes to the local LLM, which answers questions, calculations and curiosities conversationally (offline) — Laya routes commands to their pipelines and questions to chat |
| *"Alza il volume"* (Turn the volume up) / *"Muto"* (Mute) | real audio control (pycaw) |
| *"Metti il volume al 30"* (Set the volume to 30) | absolute level (digits, words or %) or relative, with verification of the achieved value |
| *"Abbassa il volume di Discord al 30%"* (Lower Discord's volume to 30%) | **per-app volume**: adjusts the single process audio session (the Windows mixer), not the system volume — only if the app is currently playing audio |
| *"Elenca i file sul desktop"* (List the files on the desktop) | reads the real folder |

At startup the server indexes the whole PC into a **library** (Start menu and desktop
shortcuts, Microsoft Store/AppX apps, portable executables with no registry entry) and
reads the **native libraries of game launchers**: Steam `.acf` manifests, Epic `.item`,
GOG `.info`, plus Riot/EA/Ubisoft/Battle.net launchers. The library gives the AI context
(to open the right app) and powers the "what games/apps do I have" command.

The desktop widget: a transparent floating draggable circle; on mouse hover a compact
card **expands out of the circle** (smooth animation, 40 pre-rendered frames) and
collapses back into it when the mouse leaves — at startup only the circle is visible.
The card has: **T** = write to Ugo, **•••** = settings, a microphone button = passive
listening on/off, **×** = collapse; every control shows a tooltip. Answers appear in
a self-dismissing bubble.

**Automatic input correction**: every command — spoken *or typed* — goes through Qwen
before execution, which cleans up misheard/mistyped words and garbled app names
(`apri spotrifyt` → `apri spotify`). Corrections that would lose an intent, a location
(`desktop`, `documenti`…) or a known site are rejected; if the app name is beyond
repair, a fuzzy fallback finds the closest app in the library. When a correction is
applied, the original transcript shows up small in the widget bubble and in the web UI
(🎧 "…").

**Voice confirmation**: if Qwen rewrites the transcript so radically that it no longer
resembles the original (similarity below threshold), Ugo executes nothing and asks
*'Did you say …? Answer yes or no'* — a spoken yes (even garbled: 'confirmo' counts)
executes the proposed command, a no cancels it; after 90 seconds the request expires
and the next command goes through normally.

**Typo memory**: every applied correction — or user-confirmed one — is saved to
`%LOCALAPPDATA%\ugo\learned_fixes.json` (max ~200 entries, sorted by frequency) and
reused twice: as an **instant** correction when the same typo reappears (zero Qwen
calls, ~0 ms instead of ~500) and as **few-shot examples** in Qwen's prompt, so it
keeps applying the same corrections you approved. The correction prompt also receives
only the **relevant apps** for the spoken words (fuzzy match on the index) instead of
an arbitrary subset of the library.

**App-alias memory**: when Qwen says "I have no app called X. Did you mean Y?" and you
answer **yes**, the pair *X → Y* is memorized: the second time, "open X" opens Y
**directly** (~40 ms, zero LLM, `alias` detector). A "no" deletes the alias if it had
been saved by mistake. Aliases live in the same `__apps__` section of the memory file.

**Pre-intent memory resolution**: confirmed typos don't wait for the full pipeline.
Memory is consulted **before the intent**, on three levels:

1. **Fastlane** (Vosk, ~0.3 s, even before Whisper): "apri spotifi" is rewritten to
   "apri Spotify" from memory and executed immediately (`fastlane` detector) — when
   the name resolves unambiguously in the app index
2. **Pre-intent phase**: wake word residues ("ugo apri spotify", "ehi ugo apri steam")
   are cleaned (`_strip_wake`), known typos rewritten and if the name resolves in the
   index the command runs in ~ms without Whisper or Qwen (`learned` detector)
3. **Inside `open_app`**: the fuzzy match on the index receives the already-corrected
   name from memory (per-token, 0.82 matching), so even unseen variants of a known
   typo resolve locally

**Yes/no guard**: confirmation answers ("yes", "no", "ok fine", up to 3 words) are
recognized only when they contain no action verb — before, "vai e apri spotify" (go
and open Spotify) was mistaken for a confirmation because of "vai" and the phrase
vanished without executing anything. Sentences with open/close/create/search… are
never confirmations.

**Numbered voice choice**: when the spoken name is ambiguous (several very similar
apps: Steam/Stremio/Stream Deck, or alternatives proposed by Qwen) the answer is a
menu — *"Which one did you mean: 1) Stremio or 2) Steam or 3) Stream Deck?"* — and you
answer **"primo", "seconda", "numero 3", "ultimo"** (first, second, number 3, last)…
The system opens the app and **learns the alias**, so next time the typo opens it
directly. A bare "yes" accepts the first option; "no" cancels; a new command expires
the menu; if a question is pending the wake-guard lets short answers through even
without the wake word.

---

## 🏗️ Architecture / Stack

```
 ┌────────────────┐  WAV    ┌──────────────────────────┐
 │ Tkinter widget  │ ──────▶ │  FastAPI server           │
 │ (ugo_agent/  │         │  :8123                    │
 │  widget.py)     │ ◀────── │                           │
 └────────────────┘  text   │ 1. STT: Whisper           │
      ▲   bubble             │    large-v3-turbo         │
      │   TTS               │    (faster-whisper,       │
      │                     │    CTranslate2: CUDA or   │
 ┌──────────────┐           │    int8 CPU, every OS)    │
 │ Web UI       │           │    fallback: Vosk it      │
 │ (browser)    │           │ 2. STT correction:        │
 └──────────────┘           │    Qwen cleans up the     │
                            │    transcript             │
                            │    (anti-damage guards)   │
                            │ 3. Intent: rules +        │
                            │    Laya (3 levels)        │
                            │ 4. Commands→JSON:         │
                            │    Qwen2.5 1.5b           │
                            │    (Ollama, local)        │
                            │ 5. Real execution         │
                            │    on the PC + TTS        │
                            │    (Piper, local)         │
                            └──────────────────────────┘
```

| Layer | Technology | Role |
|---|---|---|
| **STT** | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) + `large-v3-turbo` CT2 (~1.6 GB): **CUDA** on NVIDIA, otherwise multi-threaded **int8 CPU** — same engine on Windows, macOS and Linux. Benchmark on Ryzen 7800X3D: int8 CPU ~4 s for a typical voice command (~2 s of audio). Small Vosk fallback | transcription in the active language (`WHISPER_LANG=auto` env for automatic detection); Vosk fallback |
| **Intent** | text rules + [Laya](https://pypi.org/project/laya/) (ModernBERT, calibrated probabilities) | classify the command in ~20 ms, 3 fallback levels; anything that is not a command is routed to the conversational pipe |
| **LLM** | Qwen2.5 via [Ollama](https://ollama.com), **selectable** size (0.5b / 1.5b / 3b — default 1.5b) | transcript correction (with anti-damage guards: intent, locations, known sites) + translation of free-form phrases into JSON specs (`create_file{name,content}`…) + 'Did you mean X?' app suggestions + **conversational answers** for anything that is not a command ("how much is 1+1", "who invented the telephone"); fuzzy `difflib` fallback on app names. Benchmark on Ryzen 7800X3D: 0.5b ~0.05 s/command but garbles correct sentences; 1.5b ~0.55 s and never touches what's right; 3b equals 1.5b at double the RAM |
| **Execution** | Python (os, subprocess, send2trash, pycaw, webbrowser) | real actions: file system, apps, sites, volume |
| **App/game libraries** | `appindex.py` + `games.py`: Start menu, Store/AppX, portable, Steam/Epic/GOG manifests | AI context, app launching, on-demand lists |
| **TTS** | [Piper](https://github.com/rhasspy/piper) local neural — **Paola** (it_IT-medium) and **Amy** (en_US-medium) voices (~63 MB each, auto-downloaded in background); selectable pyttsx3/SAPI fallback from the UI | natural offline voice reply, interrupted on new input |
| **GUI** | Tkinter stdlib + Pillow (anti-aliased icons, transparency keying) | always-on-top draggable widget, zero GUI dependencies |

---

## ⚙️ Requirements

- **Windows 10/11** (full experience) or **macOS 13+** / **Linux** (see Compatibility)
- **Python 3.10+** with pip
- **GPU**: optional — with NVIDIA (CUDA) transcription flies; on plain CPU int8 is still usable
- ~3 GB of disk for the models

## 🌍 Cross-platform compatibility

All OS differences are encapsulated in `ugo_agent/platform_utils.py` (data folders,
TTS, file launching, volume, subprocess flags): the rest of the code never branches
on `sys.platform` directly. CI verifies installation, compilation and index scanning
on Windows, macOS and Linux runners at every push.

| Feature | Windows | macOS | Linux |
|---|---|---|---|
| Server, pipeline, intent, STT correction, web UI | ✅ | ✅ | ✅ |
| Whisper STT (faster-whisper) | ✅ CUDA/CPU | ✅ CUDA/**Metal**/CPU | ✅ CUDA/CPU |
| TTS | ✅ **Piper (Paola/Amy)** + SAPI fallback | ✅ **Piper (Paola/Amy)** + NSSpeech fallback | ✅ **Piper (Paola/Amy)** + espeak-ng fallback |
| App index | Start menu, Store/AppX, portable | `/Applications` | `.desktop` (XDG) |
| Game libraries | Steam, Epic, GOG, launchers | **Steam** (same `.acf` format) | **Steam** (same format) |
| Widget | click-through transparent | semi-transparent (Aqua) | semi-transparent |
| Master volume | pycaw | — (not implemented) | pycaw / pactl / amixer (fallback) |
| **Per-app** volume (mixer) | ✅ pycaw (audio sessions) | — | — (desktop API limitation) |
| Closing apps | taskkill | `pkill` | `pkill` |
| Listing processes | tasklist | psutil | psutil |
| Sites and web searches | default browser | default browser | default browser |
| Data folders | `%LOCALAPPDATA%\ugo` | `~/Library/Application Support/ugo` | `~/.local/share/ugo` |
| Package install | **winget** (`Ugo.Agent`) + pip | pip | pip |
| Ollama installation (`ugo setup`) | winget | brew | official script |

Note: the official Systran model repo turned out to be unreachable, so we use the
community reference CT2 conversion (`deepdml/faster-whisper-large-v3-turbo-ct2`).
With an NVIDIA GPU add the CUDA cuDNN packages (see below) for acceleration;
otherwise int8 CPU.

## 📦 Installation — one command

`ugo run` does **everything automatically**: installs missing dependencies, Ollama
(via winget on Windows, brew on macOS, official script on Linux), the Qwen models
(1.5b default + 0.5b reserve), Whisper large-v3-turbo CTranslate2 (~1.6 GB, every OS)
and the Vosk fallback, then starts the server and the widget.

### Windows

**With winget** (Windows Package Manager, included in Windows 10/11). The
`Ugo.Agent` package is not yet in the winget community catalog, so today you
install it from the **manifest bundle attached to every release** (no admin
needed, one settings toggle the first time):

```bat
:: 1. download ugo-<version>-winget-manifests.zip from
::    https://github.com/giovannimazza/ugo-local-ai-agent/releases/latest
::    and extract the three .yaml files into a folder (winget downloads
::    the installer itself from the release assets and verifies its SHA256)
:: 2. allow local manifests (once, from an ADMIN terminal):
winget settings --enable LocalManifestFiles
:: 3. install from a NORMAL (non-admin) terminal — portable packages refuse
::    to install elevated, and --manifest takes NO package id next to it:
winget install --manifest <folder-with-the-yaml-files>
```

This puts the `ugo` launcher on the PATH (portable package, no admin; it also
shows up in "Installed apps", uninstall with `winget uninstall Ugo.Agent`).
On the first `ugo run` the launcher provisions everything by itself: Python 3.10+
(installed via winget if missing), the ugo-agent package from GitHub and then,
as usual, the local models (Whisper, Qwen, Piper). Updates stay automatic
(`ugo update`); `winget upgrade` will work once the manifests of a newer release
are installed the same way (or after the package is accepted into the
[winget-pkgs](https://github.com/microsoft/winget-pkgs) community catalog, when
a plain `winget install Ugo.Agent` will be enough).

**With pip**:

```bat
pip install git+https://github.com/giovannimazza/ugo-local-ai-agent.git
ugo run
```

### macOS

```bash
pip3 install git+https://github.com/giovannimazza/ugo-local-ai-agent.git
ugo run
```

First time on macOS: if `brew` is missing install it from [brew.sh](https://brew.sh),
then grant microphone permission when macOS asks on first launch.

### Linux

```bash
sudo apt install python3-pip espeak-ng libportaudio2
pip3 install git+https://github.com/giovannimazza/ugo-local-ai-agent.git
ugo run
```

`espeak-ng` provides the TTS voice, `libportaudio2` the microphone; on non-Debian
distros use your package manager's equivalent.

Available commands:

| Command | Effect |
|---|---|
| `ugo run` | starts everything (installs what's missing first) |
| `ugo run server` | server only, no widget |
| `ugo start` | synonym of `run` |
| `ugo setup` | installation only, without starting |
| `ugo doctor` | diagnostics: what's installed and what's missing |
| `ugo stop` | stops the widget and the server |
| `ugo log` | opens a terminal showing the passive listening live feed |
| `ugo update` | checks GitHub and updates to the latest version of the channel |
| `ugo channel` | shows/switches the update channel (`dev` or `stable`) |
| `ugo version` | shows the installed version |
| `ugo winget` | info about the winget installation/update (Windows) |
| `ugo help` | full command list with explanations and examples |

On first `setup`/`run` the CLI automatically adds the `ugo` command to the PATH (pip
Scripts directory on Windows, with a process notification — open a new terminal; on
macOS/Linux it creates a launcher in `~/.local/bin`), so you can call it from any
folder.

### Automatic updates

At every start (`ugo run` or double-click on `ugo_app.py`) Ugo checks GitHub for a
newer version. For a git clone the comparison is on **commits** (`git fetch` with your
saved credentials: works with private repos too); for direct pip installations it
compares versions in the remote `pyproject.toml`.

- **From a terminal**: asks for confirmation (`Update now? [y/N]`) before
  `git pull` + package reinstall, then restarts the components with the new code
- **Double-click** (no console): updates silently and restarts by itself
- Uncommitted local changes are stashed and **restored** after the update, never lost
- Offline or unreachable repo: the check is skipped, never a blocker
- Manual updates always possible: `ugo update`

### Update channels and releases

| Channel | What you get | Command |
|---|---|---|
| `dev` (default) | the latest code on `main`, at every start | `ugo channel dev` |
| `stable` | official releases only (tag `v*`): zero surprises, easy rollback | `ugo channel stable` |

Every release is born by pushing a tag aligned with the pyproject version
(`git tag v0.3.0 && git push origin v0.3.0`): a GitHub workflow creates the release
with automatic notes, and on the stable channel `ugo update` brings the code exactly
to that tag. To follow development again: `ugo channel dev`.

#### Rolling back to a previous version

The stable channel makes rollback trivial: every released version stays tagged on
GitHub and `ugo update` always brings the latest — to go back, just check out a
specific tag:

```bash
# 1. stop the components
ugo stop

# 2. bring the code exactly to the tag of the version you want (e.g. v0.3.0)
git checkout v0.3.0

# 3. reinstall and restart
pip install -e .
ugo run

# to get back to the latest available version:
git checkout main && git pull
```

Uncommitted local changes survive all these steps (stash/checkout carry them along);
typo memory, routines and preferences live in `%LOCALAPPDATA%/ugo` and are never
touched by a rollback.

### Manual installation (alternative)

```bat
:: 1. Python dependencies
pip install fastapi uvicorn laya pyttsx3 vosk soundcard numpy pillow send2trash faster-whisper pycaw comtypes

:: 2. Ollama + models (1.5b is the default, 0.5b the reserve)
winget install Ollama.Ollama
ollama pull qwen2.5:1.5b
ollama pull qwen2.5:0.5b

:: 3. Whisper large-v3-turbo CTranslate2 (~1.6 GB, every OS)
:: also downloaded automatically by `ugo setup`/`ugo run`
huggingface-cli download deepdml/faster-whisper-large-v3-turbo-ct2 ^
  --local-dir %USERPROFILE%\.cache\whisper\faster-whisper-large-v3-turbo
```

> Vosk (STT fallback) downloads the `vosk-model-small-it-0.22` model into
> `~/.cache/vosk/` at first start if missing; without Whisper's CT2 model only Vosk
> is used. With an NVIDIA GPU install the CUDA cuDNN packages for acceleration:
> `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*`

## 🚀 Manual start (without the CLI)

```bat
:: one command: cleans up previous instances and opens server + widget
double-click on ugo_app.py     (or: python ugo_app.py)

:: with the console you see the startup messages:
python ugo_app.py
:: server only, no widget:
python ugo_app.py --server
:: clean server restart but reuse of the active one if healthy:
python ugo_app.py --reuse

:: manual alternative:
python ugo_agent\server.py     :: terminal 1
pythonw ugo_agent\widget.py    :: terminal 2
```

If port 8123 is occupied by a previous instance it is **terminated automatically**
(only if it's a Python process: a foreign program on the port is respected and
reported) and everything restarts clean; duplicate desktop widgets are closed and a
single one remains.

> The old root scripts `voice_assistant_server.py` and `assistant_widget.py` are
> backward-compatible shims pointing at the package. On macOS/Linux: `python3
> ugo_agent/server.py` and `python3 ugo_agent/widget.py`.
>
> Runtime files (index caches, wav, positions): `%LOCALAPPDATA%\ugo` on Windows,
> `~/Library/Application Support/ugo` on macOS, `~/.local/share/ugo` on Linux.

### Using the widget
- **Click** on the circle → record; **second click** → send
- **Passive listening** 🎙️: say **"Ugo"** (or *ehi/oh/a Ugo*) and immediately the
  command — *"Ugo apri Spotify"* — without touching anything. Streaming Vosk, near
  zero CPU; it pauses during manual recording and for a few seconds after every
  answer (so Ugo's own voice doesn't retrigger it). The microphone button on the hover
  card (bottom row, center) turns passive listening off/on
- **Whisper wake-guard**: the cheap detector in the widget (Vosk) can mistake TV,
  conversations or noise for the wake word. The server verifies with **Whisper
  large-v3-turbo** that "Ugo" (or one of its garblings: *uga, oga, u go, sugo…*) is
  really in the phrase **before executing**; false positives are silently discarded
  (no bubble, no voice). Only active on passive-listening submissions (`?wake=1`):
  dictation and manual microphone are unaffected
- **Neural wake word (experimental)**: `python -m ugo_agent.ww_collect` records ~40
  "Ugo" + ~40 negative phrases with your voice (every positive is verified with
  Whisper, fuzzy edit distance ≤ 2); `python -m ugo_agent.ww_train` trains a custom
  openWakeWord classifier (ONNX, input `(N,16,96)`) on real + synthetic Piper clips
  and picks the streaming threshold. The widget loads it **only** if it passes
  validation (TPR ≥ 60% at FPR 0, declared in `ww_ugo.json`): otherwise the Vosk +
  Whisper wake-guard duo stays
- **Automatic microphone choice**: if the default device is mute (e.g. an audio
  interface with no input plugged in), the widget probes the inputs and uses the live
  one; the choice is remembered. You can also pick the input **manually** from the
  settings menu (•••), separately for passive listening and manual recording —
  the switch is hot, no widget restart needed
- **Mouse over** → the card expands from behind the circle: **T** = write to Ugo
  (Enter = send, Esc = close; an unsent draft is kept even if the mouse leaves),
  **•••** = settings menu (transcriber info, input microphone, TTS mute 🔊/🔇,
  AI model),
  **mic button** = passive listening toggle 🎙️/🚫🎙️, **×** = collapse —
  with tooltips on every control
- **Double-click** → settings menu (active transcriber, input microphone, TTS mute, AI model)
- **Drag** the widget anywhere (position is remembered)
- **Right-click** → close

### Web UI
A ChatGPT-style chat interface on `http://127.0.0.1:8123`: messages with avatars,
hero with suggested commands, microphone in the composer. The **🎙️ button in the
top bar** picks the input device (browser device list, remembered per browser via
localStorage; the browser may also ask for permission). Replies containing lists
(games, apps) open a **modal** with the full list; the **🗂️ Apps & games** button
(pinned top-right) opens the complete library with **collapsible** categories
(Games / Applications / System tools) and **name search**. The **🌐 flag** switches
between Italian and English (UI, widget, transcription language and voice).

### Routines (voice macros) ⚡
One activation phrase runs a sequence of commands in order:

- **By voice**: *"Ugo, quando dico modo gaming esegui apri steam; apri discord;
  volume 80"* (Ugo, when I say gaming mode run open steam; open discord; volume 80)
  (separators: `;`, "e poi", "poi" — max 8 steps). Then "modo gaming" runs it all.
- **From the web**: the **⚡ Routines** button → create, edit, run (▶) and delete.
- They live in the same on-disk memory as typos/aliases (the `__routines__`
  section): they survive restarts and are shared between voice and interface.
- Before every reply the executor consults routines: the trigger may have a tail
  ("modo gaming attivato" / "gaming mode activated") and the longest trigger wins on
  overlap.
- For **one-off** chains you don't need a routine anymore: *"apri youtube e poi
  discord"* runs both on the fly (multi-command splitter), while routines keep
  their reusable trigger and can chain up to 8 named steps.

### Latency dashboard 📊
The **📊** button on top shows in real time (3 s refresh) where time goes at each
stage: Vosk pre-read, fastlane, Whisper, Qwen (correction/intent/suggestion), Piper
TTS and the **full command total** — average, p95, max and sample count (last 50 per
stage), with comparison bars, active models and process state (RAM/CPU/threads).
Collection can be paused from the panel itself.

### API
```bash
curl -X POST http://127.0.0.1:8123/api/text -H "Content-Type: application/json" ^
     -d '{"text":"crea una cartella chiamata Prova sul desktop"}'
```

| Endpoint | Use |
|---|---|
| `POST /api/text` | executes a text command |
| `POST /api/listen` | webm audio from the browser (via ffmpeg) |
| `POST /api/listen_wav` | PCM WAV from the widget |
| `GET /api/apps[?q=term]` | app + game library, with categories (optional search) |
| `POST /api/apps/rescan` | re-indexes the apps |
| `GET /api/list` | last game/app list requested by voice |
| `GET /api/stt` | active transcriber (engine, model, device) |
| `POST /api/normalize` | corrects a transcript with Qwen without executing: `{raw, text, corrected}` |
| `GET/POST /api/model` | active LLM model / switch model (persisted, menu in widget and UI) |
| `GET/POST /api/lang` | active language (it/en) / switch language: voice, Whisper and UI follow |
| `GET/POST /api/routines` | routines (voice macros): list / create·update·delete·run |
| `GET/POST /api/stats` | latency dashboard (avg/p95 per stage, active models, process) / pause-resume collection |
| `GET /_tts_reply.wav` | last spoken reply |

## 📁 Project structure

```
ugo_agent/
├── server.py         # FastAPI: STT (faster-whisper/Vosk), intent, LLM, execution, TTS, API
├── widget.py         # Tkinter desktop widget (transparent, draggable)
├── ui.html           # ChatGPT-style web UI
├── cli.py            # ugo run/setup/doctor/stop commands
├── platform_utils.py # OS abstractions: folders, TTS, open, volume, subprocess
├── appindex.py       # app library: lnk/Store/portable, /Applications, .desktop
└── games.py          # game libraries: Steam (win/mac/linux), Epic, GOG
```

Runtime files (index caches, wav, positions) in `%LOCALAPPDATA%\ugo` (Windows),
`~/Library/Application Support/ugo` (macOS) or `~/.local/share/ugo` (Linux).

## 🍎 Platform notes

- **Windows**: full experience — Whisper via faster-whisper (CUDA or CPU),
  click-through transparent widget, SAPI TTS with Italian voices, master and
  **per-application** volume control (pycaw audio session mixer), taskkill app
  closing.
- **macOS**: **Whisper works here too**: faster-whisper runs on Metal (configurable)
  or CPU, TTS with the system voice, semi-transparent widget (Aqua has no color
  keying), games read from Steam's `.acf` manifests
  (`~/Library/Application Support/Steam`). On first launch grant the microphone in
  System Settings → Privacy & Security.
- **Linux**: like macOS for STT/TTS; widget with partial transparency, games via
  Steam (`~/.steam`), app index from XDG `.desktop` files. On Wayland the widget's
  always-on-top may depend on the compositor. Master volume with pycaw or
  `pactl`/`amixer` fallback; app closing via `pkill`.
- **Everywhere**: server, pipeline (intent + correction + voice confirmation), web UI
  and every endpoint are identical — only the system "skin" changes.
- **Whisper acceleration**: `WHISPER_DEVICE=auto|cuda|cpu` (default: CUDA if present),
  `WHISPER_COMPUTE=default|int8|...`, `WHISPER_LANG=it|en|auto` (defaults to the UI
  language).
  On macOS Metal is possible via `pip install ctranslate2` with Metal support
  (experimental) or `WHISPER_DEVICE=cpu`.

## 🛠️ Extending

- **New apps/sites**: `APP_ALIAS` / `SITE_ALIAS` dictionaries in `ugo_agent/server.py`
- **New commands**: add keywords in `KEYWORDS` + a branch in `run_command()`
- **More game launchers**: add a collector in `ugo_agent/games.py`
- **Disabling Whisper**: environment variable `WHISPER=0` (Vosk only)
- **Whisper device/compute**: env `WHISPER_DEVICE`, `WHISPER_COMPUTE`, `WHISPER_LANG` (see Platform notes)
- **Microphone/latency**: the Whisper model stays resident in RAM/VRAM; cold start ~1 s

## 📄 License

Personal use. Models: Whisper (MIT/OpenAI), Qwen2.5 (Apache 2.0), Laya (Apache 2.0).
