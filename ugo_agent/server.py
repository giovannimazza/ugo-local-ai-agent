# -*- coding: utf-8 -*-
"""
Assistente vocale locale con UI web.

Flusso: microfono -> STT offline (Vosk, italiano) -> intent (Laya, locale)
      -> esecuzione comando reale sul PC -> risposta TTS offline (Elsa, italiano).

Comandi supportati:
  - "crea una cartella chiamata Prova sul desktop"
  - "elimina la cartella Prova"            (nel cestino, recuperabile)
  - "apri calcolatrice / blocco note / youtube / spotify ..."
  - "che ore sono?" / "che giorno e' oggi?"
  - "alza il volume / abbassa il volume / muto"
  - "elenca i file sul desktop" / "riavvia il riconoscimento"

Avvio:  python voice_assistant_server.py   ->  http://127.0.0.1:8123
"""

try:  # pacchetto (pip install / -m / uvicorn) O script diretto (python ugo_agent/server.py)
    from . import games
    from . import appindex
except ImportError:
    import games
    import appindex
import difflib
import io
import json
import numpy as np
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import wave
import webbrowser
from datetime import datetime
from pathlib import Path

try:  # pacchetto (pip install / -m) O script diretto (python ugo_agent/server.py)
    from . import platform_utils as pu
    from . import piper_tts
    from . import multicommand
except ImportError:
    if __package__ is None and str(Path(__file__).resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ugo_agent import platform_utils as pu
    from ugo_agent import piper_tts
    from ugo_agent import multicommand

import laya
import pyttsx3
import send2trash

# ---------------------------------------------------------------------------
# subprocess SENZA finestra: su Windows qualunque console (taskkill, powershell,
# ffmpeg, piper...) fa lampeggiare un terminale nero se non lo si nasconde
# ---------------------------------------------------------------------------
_WFLAGS = {
    "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
} if pu.IS_WINDOWS else {}


def _run(cmd, **kw):
    """subprocess.run senza finestra console (Windows)."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.update(_WFLAGS)
    return subprocess.run(cmd, **kw)


def _popen(cmd, **kw):
    """subprocess.Popen senza finestra console (Windows)."""
    kw.update(_WFLAGS)
    return subprocess.Popen(cmd, **kw)


from fastapi import FastAPI, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from vosk import KaldiRecognizer, Model as VoskModel
from faster_whisper import WhisperModel

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
PORT = 8123
PKG_DIR = Path(__file__).resolve().parent
BASE = pu.data_dir()
BASE.mkdir(parents=True, exist_ok=True)
VOSK_MODEL_DIR = os.path.expanduser("~/.cache/vosk/vosk-model-small-it-0.22")
HOME = Path.home()
CONF_THRESHOLD = 0.70  # sotto questa confidence Laya -> risposta "non ho capito"

APP_ALIAS = {
    "calcolatrice": "calc.exe",
    "calculator": "calc.exe",
    "blocco note": "notepad.exe",
    "notepad": "notepad.exe",
    "paint": "mspaint.exe",
    "esplora risorse": "explorer.exe",
    "explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "gestione attivita": "taskmgr.exe",
    "cmd": "cmd.exe",
    "terminale": "cmd.exe",
    "prompt dei comandi": "cmd.exe",
    "spotify": ["spotify"],
    "comet": ["comet"],
    "chrome": ["chrome"],
    "edge": ["msedge"],
    "word": ["winword"],
    "excel": ["excel"],
    "vscode": ["code"],
    "visual studio code": ["code"],
    "vlc": ["vlc"],
}
SITE_ALIAS = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "wikipedia": "https://it.wikipedia.org",
    "github": "https://github.com",
    "chatgpt": "https://chat.openai.com",
    "whatsapp": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "amazon": "https://www.amazon.it",
}
_UF = pu.user_folders()
FOLDER_MAP = {
    "desktop": _UF.get("desktop", HOME / "Desktop"),
    "scrivania": _UF.get("desktop", HOME / "Desktop"),
    "documenti": _UF.get("documents", HOME / "Documents"),
    "documents": _UF.get("documents", HOME / "Documents"),
    "download": _UF.get("downloads", HOME / "Downloads"),
    "scaricati": _UF.get("downloads", HOME / "Downloads"),
    "immagini": HOME / "Pictures",
    "pictures": HOME / "Pictures",
    "musica": HOME / "Music",
    "video": HOME / "Videos",
    "home": HOME,
}
PLACE_WORDS = {  # parole comuni che NON sono nomi di cartelle
    "una", "un", "il", "lo", "la", "le", "i", "gli", "nuova", "nuovo",
    "cartella", "directory", "chiamata", "chiamato", "chiamato", "nome",
    "sul", "sulla", "nel", "nella", "in", "su", "del", "della", "dei",
    "per", "favore", "potresti", "puoi", "voglio", "vorrei", "fammi",
    "che", "si", "prego", "adesso", "ora", "poi", "dopo", "con", "di",
}
DEFAULT_LOCATION = HOME / "Desktop"


# ---------------------------------------------------------------------------
# Collegamenti del menu Start: per aprire QUALSIASI app installata (Steam, Epic...)
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _start_menu_dirs() -> list:
    dirs = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            p = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if p.is_dir():
                dirs.append(p)
    desktop = HOME / "Desktop"
    if desktop.is_dir():
        dirs.append(desktop)
    return dirs


_LNK_CACHE = {"when": 0.0, "files": []}


def _find_shortcut(target: str) -> Path | None:
    """Cerca il collegamento .lnk che somiglia di piu' al nome richiesto."""
    now = time.time()
    if now - _LNK_CACHE["when"] > 300 or not _LNK_CACHE["files"]:
        files = []
        for d in _start_menu_dirs():
            try:
                files.extend(d.rglob("*.lnk"))
            except Exception:
                pass
        _LNK_CACHE.update(when=now, files=files)
    t = _norm(target)
    if not t:
        return None
    best, best_score = None, 0.0
    for f in _LNK_CACHE["files"]:
        name = _norm(f.stem)
        if name == t:
            score = 3.0
        elif t in name:
            score = 2.0 + min(1.0, len(t) / max(1, len(name)))
        elif name in t:
            score = 1.0 + min(1.0, len(name) / max(1, len(t)))
        else:
            score = 0.0
        if score > best_score:
            best, best_score = f, score
    return best if best_score >= 2.0 else None

# ---------------------------------------------------------------------------
# Stato globale
# ---------------------------------------------------------------------------
app = FastAPI(title="Assistente Vocale Locale")
_log_lock = threading.Lock()
_tts_lock = threading.Lock()
_history = []           # chat per la UI
_last_items = []        # ultima lista giochi/app prodotta (per la modale UI)
_stt = {"model": None, "recognizer": None}
_laya_router = None

# ---------------------------------------------------------------------------
# Dashboard latenze: per ogni fase della pipeline (whisper, vosk, qwen,
# tts, fastlane, wakeup-guard, comando) misuriamo durate e teniamo gli
# ultimi 50 campi. La UI li mostra con media/p95 e modelli attivi.
# ---------------------------------------------------------------------------
_stats_lock = threading.Lock()
_stats: dict[str, list[float]] = {}
_stats_enabled = {"on": True}   # la UI puo' sospendere la raccolta

# log delle chiamate API e risposte (per 'ugo server log'): righeleggibile,
# con rotazione a ~1 MB per non crescere all'infinito
_REQLOG = BASE / "server_log.txt"
_reqlog_lock = threading.Lock()


def _reqlog(msg: str) -> None:
    try:
        with _reqlog_lock:
            if _REQLOG.exists() and _REQLOG.stat().st_size > 1_000_000:
                _REQLOG.replace(_REQLOG.with_suffix(".txt.1"))
            with _REQLOG.open("a", encoding="utf-8") as f:
                f.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
    except Exception:
        pass  # il logging non deve mai rompere la pipeline


def _track(stage: str, dt: float) -> None:
    """Registra la durata (secondi) di una fase; i contatori globali di
    processo (rss, cpu) vengono campionati al momento della richiesta stats."""
    if not _stats_enabled["on"]:
        return
    with _stats_lock:
        buf = _stats.setdefault(stage, [])
        buf.append(dt)
        if len(buf) > 50:
            del buf[:-50]


_tps: dict[str, list] = {}   # token/s delle fasi LLM (da eval_count/eval_duration)


def _track_tps(stage: str, eval_count: int, eval_duration_ns: int) -> None:
    """Token/s reali riportati da Ollama per la chiamata appena conclusa."""
    if not _stats_enabled["on"] or not eval_count or not eval_duration_ns:
        return
    tps = eval_count / (eval_duration_ns / 1e9)
    with _stats_lock:
        buf = _tps.setdefault(stage, [])
        buf.append(tps)
        if len(buf) > 50:
            del buf[:-50]

# ---------------------------------------------------------------------------
# Whisper via faster-whisper (CTranslate2): CUDA/CPU su Windows, Metal/CPU su
# macOS, CUDA/CPU su Linux — stesso motore ovunque. Il modello e' SCEGLIBILE
# da web UI /api/stt/models (download on-demand) e la scelta resta su disco.
# Se il modello non c'e' o WHISPER=0, si ricade su Vosk piccolo.
# ---------------------------------------------------------------------------
WHISPER_CATALOG = {
    "large-v3-turbo": {
        "label": "large-v3-turbo — la migliore (~1.6 GB)",
        "dir": "~/.cache/whisper/faster-whisper-large-v3-turbo",
        "repo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    },
    "small": {
        "label": "small — compromesso veloce (~500 MB)",
        "dir": "~/.cache/whisper/faster-whisper-small",
        "repo": "Systran/faster-whisper-small",
    },
    "base": {
        "label": "base — molto rapida, qualita' base (~150 MB)",
        "dir": "~/.cache/whisper/faster-whisper-base",
        "repo": "Systran/faster-whisper-base",
    },
}
WHISPER_DEFAULT = "large-v3-turbo"
WHISPER_FILES = ("config.json", "model.bin", "preprocessor_config.json",
                 "tokenizer.json", "vocabulary.json")
_whisper_choice = {"name": WHISPER_DEFAULT}
STT_FILE = BASE / "stt.json"
try:
    if STT_FILE.exists():
        _n = json.loads(STT_FILE.read_text()).get("whisper")
        if _n in WHISPER_CATALOG:
            _whisper_choice["name"] = _n
except Exception:
    pass


def whisper_model_dir() -> str:
    """Cartella del modello Whisper attivo (la scelta e' cambiabile a caldo)."""
    return os.path.expanduser(WHISPER_CATALOG[_whisper_choice["name"]]["dir"])


_whisper = {"model": None}
_whisper_lock = threading.Lock()
_whisper_dl = {"busy": set()}   # modelli in download in questo momento

try:
    laya_system = laya.load("convaiinnovations/laya")  # checkpoint inglese
except Exception as exc:  # laya opzionale: senza, si usa solo il matching testuale
    print(f"[laya] non disponibile ({exc}); uso solo regole testuali")
    laya_system = None

print("[apps] scansione libreria applicazioni in background...")
appindex.scan_async()  # lnk + Microsoft Store/AppX + portabili, all'avvio
piper_tts.ensure_started()  # scarica in bg la voce naturale se manca (poi parla Paola)


# ---------------------------------------------------------------------------
# TTS: Piper neurale (Paola, naturale) con fallback sulla voce di sistema
# (SAPI/Elsa su Windows, NSSpeech/espeak altrove). Il download di Piper parte
# in background allo startup: finche' non e' pronto parla la voce di sistema.
# ---------------------------------------------------------------------------
def _pick_voice(engine) -> None:
    """Voce di sistema coerente con la lingua attiva (it: Elsa/Italian;
    en: Zira/David/English)."""
    want_en = piper_tts.current_lang() == "en"
    for v in engine.getProperty("voices"):
        name = (v.name or "").lower()
        vid = str(getattr(v, "id", "")).lower()
        if want_en:
            if "english" in name or "en-us" in vid or "en_us" in vid or "zira" in name or "david" in name:
                engine.setProperty("voice", v.id)
                return
        else:
            if "ital" in name or "elsa" in name or "it-it" in vid:
                engine.setProperty("voice", v.id)
                return


def speak(text: str) -> str:
    """Riproduce la risposta a voce e ritorna il path del file wav generato."""
    out = BASE / "_tts_reply.wav"
    t0 = time.time()
    with _tts_lock:
        try:
            if piper_tts.synthesize(text, out):
                _track("tts_piper", time.time() - t0)
                return str(out)          # voce naturale: fatta
        except Exception as exc:
            print(f"[piper] inatteso ({exc}); passo alla voce di sistema")
        try:
            engine = pyttsx3.init()
            _pick_voice(engine)
            engine.setProperty("rate", 175)
            engine.save_to_file(text, str(out))
            engine.runAndWait()
            _track("tts_sapi", time.time() - t0)
        except Exception as exc:
            # fuori da Windows pyttsx3 usa espeak/NSSpeechSynthesizer: se mancano,
            # la risposta resta scritta (bolla/UI) invece di rompere la pipeline
            print(f"[tts] motore non disponibile ({exc}); risposta solo testuale")
            out.write_bytes(b"")
    return str(out)


# ---------------------------------------------------------------------------
# STT (Vosk, italiano, offline)
# ---------------------------------------------------------------------------
def get_stt():
    if _stt["model"] is None:
        print("[stt] caricamento modello Vosk italiano...")
        _stt["model"] = VoskModel(VOSK_MODEL_DIR)
    return _stt["model"]


def get_whisper():
    """Carica Whisper una sola volta: CUDA (NVIDIA) se presente, CPU altrimenti."""
    if _whisper["model"] is None:
        with _whisper_lock:
            if _whisper["model"] is None:
                dev = os.environ.get("WHISPER_DEVICE", "auto")
                if dev == "auto":
                    try:
                        import ctranslate2 as ct
                        dev = "cuda" if ct.get_cuda_device_count() > 0 else "cpu"
                    except Exception:
                        dev = "cpu"
                print(f"[whisper] caricamento {_whisper_choice['name']} "
                      f"(faster-whisper, device={dev})...")
                kw = {"compute_type": os.environ.get("WHISPER_COMPUTE", "default")}
                if dev == "cpu":
                    kw["cpu_threads"] = min(8, os.cpu_count() or 4)  # benchmark: ottimo su Zen4
                _whisper["model"] = WhisperModel(whisper_model_dir(), device=dev, **kw)
    return _whisper["model"]


def whisper_available() -> bool:
    return (os.environ.get("WHISPER", "1") == "1"
            and os.path.isdir(whisper_model_dir())
            and os.path.isfile(os.path.join(whisper_model_dir(), "model.bin")))


def _fw_installed(name: str) -> bool:
    d = os.path.expanduser(WHISPER_CATALOG[name]["dir"])
    return os.path.isfile(os.path.join(d, "model.bin"))


def _fw_download(name: str) -> None:
    """Scarica i file CT2 del modello in background (chiamato in un thread)."""
    info = WHISPER_CATALOG[name]
    d = Path(os.path.expanduser(info["dir"]))
    d.mkdir(parents=True, exist_ok=True)
    base = f"https://huggingface.co/{info['repo']}/resolve/main/"
    try:
        for fname in WHISPER_FILES:
            dest = d / fname
            if dest.exists():
                continue
            print(f"[whisper] scarico {name}: {fname}...")
            urllib.request.urlretrieve(base + fname, str(dest) + ".part")
            Path(str(dest) + ".part").rename(dest)
        print(f"[whisper] modello {name} pronto")
    except Exception as exc:
        print(f"[whisper] download {name} fallito: {exc}")
    finally:
        _whisper_dl["busy"].discard(name)


def _whisper_transcribe(pcm16: bytes) -> str:
    """PCM s16le 16k mono -> testo con Whisper turbo."""
    a = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    # la lingua segue l'impostazione globale (bandiera UI); env override possibile
    lang = os.environ.get("WHISPER_LANG") or piper_tts.current_lang()
    t0 = time.time()
    segments, _info = get_whisper().transcribe(
        a, language=None if lang == "auto" else lang,
        beam_size=1, vad_filter=True, condition_on_previous_text=False)
    text = " ".join(s.text for s in segments).strip()
    _track("whisper", time.time() - t0)
    return text


def _vosk_transcribe(pcm: bytes) -> str:
    """Trascrive PCM s16le 16 kHz mono con Vosk; riconoscitore fresco per richiesta."""
    t0 = time.time()
    rec = KaldiRecognizer(get_stt(), 16000)
    rec.AcceptWaveform(pcm)
    txt = json.loads(rec.FinalResult()).get("text", "").strip()
    _track("vosk", time.time() - t0)
    return txt


def transcribe(pcm: bytes) -> str:
    """Whisper prima (qualita' alta, GPU), Vosk come fallback."""
    if whisper_available():
        try:
            text = _whisper_transcribe(pcm)
            if text:
                return text
        except Exception as exc:
            print(f"[whisper] errore: {exc}; fallback Vosk")
    return _vosk_transcribe(pcm)


# ---------------------------------------------------------------------------
# Laya: classifica l'intento tra i tipi di comando
# ---------------------------------------------------------------------------
LAYA_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "What action is the user asking the voice assistant to perform?",
        "criteria": {
            "create_folder": "create a new folder somewhere",
            "delete_folder": "delete or remove an existing folder",
            "open_app": "open or launch a program or application",
            "open_site": "open a website or go to a web page",
            "time": "ask what time it is",
            "date": "ask what day or date it is today",
            "volume": "turn volume up, down or mute the computer",
            "set_app_volume": "set, raise or lower the volume of ONE specific application (e.g. lower Discord's volume to 30 percent)",
            "close_app": "close or quit a running application the user names",
            "list_files": "list or show the files in a directory",
            "list_apps": "ask which apps or games are installed (e.g. what games do I have on steam)",
            "unknown": "anything else: small talk, questions, other requests",
        },
    }
}
LAYA_LABELS = set(LAYA_QUESTIONS["intent"]["criteria"]) - {"unknown"}

# ---------------------------------------------------------------------------
# Piccolo modello LLM locale (Ollama + Qwen2.5 0.5B):
# traduce il linguaggio naturale in una "specifica comando" JSON per l'executer.
# ---------------------------------------------------------------------------
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:0.5b"   # fallback: il piu' piccolo, sempre disponibile
DEFAULT_MODEL = "qwen2.5:1.5b"  # benchmark: corretto sulle frasi giuste e sugli errori
MODEL_FILE = pu.data_dir() / "model.json"
_active_model = {"name": DEFAULT_MODEL}

try:  # scelta persistita dall'utente (menu widget/UI web)
    _saved = json.loads(MODEL_FILE.read_text()) if MODEL_FILE.exists() else {}
    if isinstance(_saved.get("model"), str) and _saved["model"]:
        _active_model["name"] = _saved["model"]
except Exception:
    pass


def ollama_models() -> list:
    """Modelli qwen* installati in Ollama (per menu e validazione)."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as r:
            tags = [m.get("name", "") for m in json.loads(r.read().decode()).get("models", [])]
        return sorted(t for t in tags if t.lower().startswith("qwen")) or [OLLAMA_MODEL]
    except Exception:
        return [OLLAMA_MODEL]


def _llm_model() -> str:
    """Modello attivo, con rientro a 0.5b se quello scelto non e' installato."""
    name = _active_model["name"]
    return name if name in ollama_models() else OLLAMA_MODEL
OLLAMA_SCHEMA = (
    'Convert an Italian voice command into ONE JSON object for a PC assistant. '
    'Allowed actions: create_folder{name,location}, delete_folder{name,location}, '
    'create_file{name,content,location}, append_file{name,content,location}, '
    'delete_file{name,location}, read_file{name,location}, open_app{app}, '
    'open_site{site}, close_app{name}, set_app_volume{app,level}, search_web{query,site}, volume{direction}, time{}, date{}, unknown{}. '
    'set_app_volume sets the audio volume of ONE application (level 0-100), not the system volume. '
    'close_app closes a running application the user asked to quit. '
    'location is one of: desktop, documents, downloads, home. '
    'For create_file and append_file the name MUST end with .txt (text file). '
    'content holds the exact text to write. If nothing fits use unknown. '
    'Answer ONLY with the JSON object. '
    'If the user asks what apps or games are installed use unknown (a rule handles it). '
    'Examples: '
    'If the user wants to open an application, prefer the exact app name from this '
    f'list of installed apps: {appindex.llm_context(limit=90)}. '
    'crea un file di testo chiamata spesa con dentro latte -> '
    '{"action":"create_file","name":"spesa.txt","content":"latte","location":"desktop"}; '
    'aggiungi al file spesa la riga uova -> '
    '{"action":"append_file","name":"spesa.txt","content":"uova","location":"desktop"}; '
    'elimina il file spesa -> {"action":"delete_file","name":"spesa.txt","location":"desktop"}; '
    'apri steam -> {"action":"open_app","app":"steam"}; '
    'leggi il file spesa -> {"action":"read_file","name":"spesa.txt","location":"desktop"}.'
)
FILE_INTENTS = {"create_file", "append_file", "delete_file", "read_file"}

# ---------------------------------------------------------------------------
# Correzione trascrizione (fase 0 della pipeline vocale):
# Qwen ripulisce gli errori dello STT (parole sentite male, ortografia di app
# e siti) PRIMA del riconoscimento dell'intento: il comando eseguito e' quello
# corretto, non quello dettato male.
# ---------------------------------------------------------------------------
TRANSLATE_SCHEMA = (
    'You translate an English voice command for a PC assistant into the '
    'equivalent Italian command, so the Italian command engine can execute it. '
    'Answer ONLY with the translated Italian command. '
    'Examples: open notepad -> apri il blocco note; open Spotify -> apri spotify; '
    'what time is it -> che ore sono; create a folder called work on the desktop '
    '-> crea una cartella chiamata work sul desktop; set volume to 50 -> volume 50. '
    'Keep app and site names as-is (Spotify, YouTube, Steam). Never execute, '
    'never answer. If unsure, answer with the original text unchanged.'
)

NORMALIZE_SCHEMA = (
    'You fix speech-to-text mistakes in an Italian voice command for a PC assistant. '
    'Given the raw transcript, answer ONLY with the corrected Italian command. '
    'Fix ONLY clearly misheard or misspelled words (e.g. made-up app names). '
    'Never change words you are not sure about. Never translate, never execute, '
    'never answer, never add or remove punctuation. Keep Italian. '
    'Never replace location words: desktop, documenti, downloads must stay exactly. '
    'Use the real names of apps and sites when the user garbles them. '
    'If the transcript is already correct or you are unsure, repeat it unchanged. '
    'Installed apps include: '
    f'{appindex.llm_context(limit=40)}. '
    'Examples: apri bre bloko nota -> apri il blocco note; '
    'apri spotifi -> apri spotify; '
    'che ore sono -> che ore sono; '
    'crea una cartella spesa sul desktop -> crea una cartella spesa sul desktop.'
)


def _relevant_apps(text: str, limit: int = 8) -> str:
    """App dell'indice piu' simili alle parole dette: da dare in contesto a Qwen,
    cosi' vede i nomi RILEVANTI e non un sottoinsieme arbitrario della libreria."""
    try:
        toks = appindex._norm(text).split()
        if not toks:
            return ""
        normed = {appindex._norm(a["name"]): a["name"] for a in appindex.get_apps()}
        picks = []
        for tok in toks:
            if len(tok) < 4:  # token corti generano falsi positivi ('ore'->Vortex)
                continue
            for m in difflib.get_close_matches(tok, list(normed), n=3, cutoff=0.65):
                if normed[m] not in picks:
                    picks.append(normed[m])
        return "; ".join(picks[:limit])
    except Exception:
        return ""


def _learned_examples(limit: int = 8) -> str:
    """Frammento di prompt con le correzioni confermate dall'utente (few-shot):
    i refusi ricorrenti vengono risolti sempre nello stesso modo."""
    data = _learned_load()
    data = {k: v for k, v in data.items() if k != "__apps__" and "raw" in v}
    if not data:
        return ""
    items = sorted(data.values(), key=lambda v: -v.get("count", 1))[:limit]
    exs = "; ".join(f"{v['raw']} -> {v['fixed']}" for v in items)
    return (" Corrections the user already confirmed in past sessions "
            f'(apply them exactly): {exs}.')


def normalize_stt(text: str) -> str | None:
    """Ritorna la trascrizione corretta da Qwen, o None se non disponibile.
    La pipeline usa il risultato solo se effettivamente diverso dall'originale.
    Con lingua EN: Qwen TRADUDE il comando inglese nell'equivalente italiano
    (l'engine di intent e di comandi e' italiano), con le stesse guardie."""
    en_mode = piper_tts.current_lang() == "en"
    print(f"[normalize] chiedo a Qwen ({_llm_model()}){' [traduco EN->IT]' if en_mode else ''}: {text!r}")
    try:
        import urllib.request
        rel = _relevant_apps(text)
        schema = (TRANSLATE_SCHEMA if en_mode else NORMALIZE_SCHEMA)
        payload = json.dumps({
            "model": _llm_model(),
            "system": (schema
                       + (f" Apps possibly mentioned: {rel}." if rel else "")
                       + _learned_examples()),
            "prompt": text,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 120},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        _q0 = time.time()
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        _track("qwen_normalize", time.time() - _q0)
        _track_tps("qwen_normalize", data.get("eval_count", 0),
                   data.get("eval_duration", 0))
        out = (data.get("response") or "").strip().strip('"').strip()
        # difese: vuoto, delirio troppo lungo o multilinea -> meglio l'originale
        if not out or "\n" in out or len(out) > len(text) * 3 + 80:
            return None
        return out
    except Exception as exc:
        print(f"[normalize] errore: {exc}")
        return None


SITI_NOTI = ("youtube", "google", "gmail", "maps", "amazon", "netflix", "twitch",
             "github", "reddit", "facebook", "instagram", "whatsapp", "spotify",
             "wikipedia", "chatgpt", "steam", "discord")
LUOGHI = ("desktop", "scrivania", "documenti", "download")


# ---------------------------------------------------------------------------
# Routine (macro vocali): frase di attivazione -> sequenza di comandi eseguiti
# in ordine. Vanno nella STESSA memoria su disco (sezione __routines__), sono
# creabili a voce ("quando dico X esegui Y; Z") o dalla pagina Routine web.
# ---------------------------------------------------------------------------
ROUTINE_MAX_STEPS = 8


def _find_routine(text: str) -> dict | None:
    """Matcha la frase contro trigger e nomi delle routine (substring sui token
    normalizzati, con tolleranza per code tipo 'modo gaming attivato')."""
    t = _norm(_strip_wake(text))
    if not t:
        return None
    rs = _learned_load().get("__routines__", {})
    best = None
    for r in rs.values():
        cands = [_norm(r.get("trigger", "")), _norm(r.get("name", ""))]
        for c in cands:
            if len(c) < 3:
                continue                 # trigger troppo corti: falsi positivi
            ctoks = c.split()
            head = " ".join(ctoks[:3])   # 'modo gaming' per 'modo gaming attivato'
            hit = (re.search(r"(?<!\S)" + re.escape(c) + r"(?!\S)", t)
                   or (len(ctoks) >= 2 and head and
                       re.search(r"(?<!\S)" + re.escape(head) + r"(?!\S)", t)))
            if hit:
                if best is None or len(c) > best[1]:
                    best = (r, len(c))
                break
    return best[0] if best else None


def _routine_execute(r: dict) -> list[str]:
    """Esegue i passi in ordine con la pipeline comandi esistente (senza
    parlare a ogni passo): ritorna i testi di risposta per il riassunto."""
    replies = []
    for s in (r.get("steps") or [])[:ROUTINE_MAX_STEPS]:
        st = (s or "").strip()
        if not st:
            continue
        try:
            intent = detect_intent(st)[0]
        except Exception:
            intent = "-"
        try:
            replies.append(run_command(st, intent))
        except Exception as exc:
            replies.append(f"{st}: errore ({type(exc).__name__})")
    return replies


def _routine_bump(rid: str) -> None:
    """Conta l'utilizzo della routine (statistica nel pannello web)."""
    with _learned_lock:
        data = _learned_load()
        rs = data.get("__routines__", {})
        if rid in rs:
            rs[rid]["count"] = rs[rid].get("count", 0) + 1
            try:
                LEARNED_FILE.write_text(json.dumps(
                    data, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Memoria delle correzioni accettate: ogni fix applicato (o confermato con
# 'si') viene salvato su disco e riusato come correzione istantanea per i
# refusi ricorrenti — Qwen 'impara' dai tuoi errori tipici di dettato.
# ---------------------------------------------------------------------------
LEARNED_FILE = BASE / "learned_fixes.json"
_learned_lock = threading.Lock()


def _learned_load() -> dict:
    try:
        return json.loads(LEARNED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _bare(s: str) -> str:
    """Testo ridotto a lettere/numeri: per confrontare ignorando maiuscole
    e punteggiatura (differenze non significative per un comando)."""
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _learn_fix(raw: str, fixed: str) -> None:
    """Registra (o rinforza) una coppia refuso -> correzione.
    Chiamata SOLO dopo conferma esplicita dell'utente ('Hai detto X?' -> si):
    niente apprendimento automatico dai cand di Qwen. In lingua EN non registra
    nulla: il testo confermato e' una TRADUZIONE (EN->IT), non un refuso, e
    memorizzarla insegnerebbe a Qwen di tradurre ogni frase inglese uguale."""
    if piper_tts.current_lang() == "en":
        return
    r, f = raw.strip(), fixed.strip()
    if len(r) < 4 or _bare(r) == _bare(f):
        return  # cambia solo maiuscole/punteggiatura: non e' un refuso
    with _learned_lock:
        data = _learned_load()
        for v in [x for x in data.values() if "raw" in x]:  # salta __apps__
            if v["raw"].lower() == r.lower():
                v["count"] = v.get("count", 1) + 1
                v["fixed"] = f
                v["ts"] = time.time()
                break
        else:
            data[r.lower()] = {"raw": r, "fixed": f, "count": 1, "ts": time.time()}
            if len(data) > 200:  # tengo le correzioni piu' usate
                keep = sorted(data.items(), key=lambda kv: -kv[1].get("count", 1))[:150]
                data = dict(keep)
        try:
            LEARNED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            print(f"[learn] memorizzata: {r!r} -> {f!r}")
        except Exception as exc:
            print(f"[learn] scrittura fallita: {exc}")


def _learn_app_alias(said: str, app_name: str) -> None:
    """Memorizza un alias-app confermato dall'utente ('Intendevi Steam?' -> si):
    la prossima volta il comando detto apre l'app direttamente, senza domanda."""
    s, a = said.strip(), app_name.strip()
    if len(s) < 3 or _bare(s) == _bare(a):
        return  # identico al nome reale: nessun apprendimento utile
    with _learned_lock:
        data = _learned_load()
        entry = data.setdefault("__apps__", {})
        entry[s.lower()] = {"said": s, "app": a, "count": entry.get(s.lower(), {}).get("count", 0) + 1,
                            "ts": time.time()}
        if len(entry) > 100:
            keep = sorted(entry.items(), key=lambda kv: -kv[1].get("count", 1))[:80]
            entry.clear()
            entry.update(keep)
        try:
            LEARNED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            print(f"[learn] alias app memorizzato: {s!r} -> {a!r}")
        except Exception as exc:
            print(f"[learn] scrittura fallita: {exc}")


def _learn_app_forget(said: str) -> None:
    """Un 'no' a 'Intendevi X?' cancella l'alias app sbagliato (se presente)."""
    with _learned_lock:
        data = _learned_load()
        entry = data.get("__apps__")
        if entry and entry.pop(said.strip().lower(), None):
            try:
                LEARNED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
            except Exception:
                pass


def _learned_app_lookup(text: str) -> str | None:
    """Se il nome detto somiglia a un alias-app confermato, ritorna l'app da aprire."""
    data = _learned_load().get("__apps__", {})
    if not data:
        return None
    t = _bare(text)
    best, score = None, 0.0
    for v in data.values():
        ratio = difflib.SequenceMatcher(None, t, _bare(v["said"])).ratio()
        if ratio > score and ratio >= 0.85:
            best, score = v["app"], ratio
    return best


def _learned_token_fix(text: str) -> str:
    """Riscrive le parole note come refusi usando la memoria ('spotifi' ->
    'Spotify'), per risolvere i nomi PRIMA dell'intent e della ricerca indice.
    La mappa viene dalle coppie apprese: parole allineate raw->fixed diverse
    tra loro (solo lunghe >= 4: le corte generano falsi positivi)."""
    tmap = {}
    for v in _learned_load().values():
        if "raw" not in v:
            continue
        rw, fw = v["raw"].split(), v["fixed"].split()
        if len(rw) == len(fw):
            for a, b in zip(rw, fw):
                if _bare(a) != _bare(b) and len(a) >= 4:
                    tmap[_bare(a)] = b
    if not tmap:
        return text
    out = []
    for w in text.split():
        b = _bare(w)
        if b in tmap:
            out.append(tmap[b])
            continue
        if len(b) >= 4:
            near = difflib.get_close_matches(b, list(tmap), n=1, cutoff=0.82)
            if near:
                out.append(tmap[near[0]])
                continue
        out.append(w)
    return " ".join(out)


def _learned_lookup(text: str) -> str | None:
    """Se il comando somiglia a un refuso gia' corretto, ritorna il fix noto."""
    data = _learned_load()
    if not data:
        return None
    t = text.strip().lower()
    best, score = None, 0.0
    for v in [x for x in data.values() if "raw" in x]:  # salta la sezione __apps__
        ratio = difflib.SequenceMatcher(None, t, v["raw"].lower()).ratio()
        if ratio > score and ratio >= 0.88:
            best, score = v["fixed"], ratio
    return best


# input che NON sono refusi di un comando: domande conversazionali e frammenti
# corti. Qwen/Laya tende a "rimediarli" in comandi plausibili (test -> 'che ore
# sono'); blocco la correzione, se il testo e' vero comando parte lo stesso.
_NOT_A_TYPO = re.compile(
    r"^\s*(quando|perche|perché|come|dove|chi|cosa|che cosa|quanto|quanta|quanti|"
    r"test|prova|abc|hello)\b", re.I)


def _semantics_changed(raw: str, cand: str) -> bool:
    """True se la 'correzione' stravolge la frase: il punto interrogativo
    (una domanda vera) sparisce, o la differenza di parole e' cosi' grande che
    la correzione inventa un altro comando (es. 'test' -> 'che ore sono')."""
    if raw.rstrip().endswith("?") and not cand.rstrip().endswith("?"):
        return True
    rw = set(_bare(raw).split())
    cw = set(_bare(cand).split())
    if not rw or not cw:
        return True
    # meno della meta' delle parole dette sopravvive nella correzione: non la accetto
    return len(rw & cw) < max(1, len(rw) // 2)


def _guards_ok(raw: str, cand: str) -> bool:
    """Guardie anti-danno condivise: la correzione non deve perdere un intent
    keyword, un luogo o un sito noto, ne' stravolgere la frase (refuso vs altro
    comando), ne' correggere input che non sono comandi. In lingua EN le guardie
    semantiche non si applicano (la traduzione EN->IT riscrive legittimamente la
    frase): vale solo il veto sull'intent keyword, che resta italiano."""
    if "->" in cand:
        return False
    raw_l, cand_l = raw.lower(), cand.lower()
    if piper_tts.current_lang() != "en":
        if _NOT_A_TYPO.match(raw_l) and _bare(raw_l) != _bare(cand_l):
            return False
        if _semantics_changed(raw_l, cand_l):
            return False
    kw_raw, kw_new = keyword_intent(raw_l), keyword_intent(cand_l)
    if kw_raw and kw_raw != kw_new:
        return False
    if any(p in raw_l for p in LUOGHI) and not any(p in cand_l for p in LUOGHI):
        return False
    if any(s in raw_l and s not in cand_l for s in SITI_NOTI):
        return False
    return True


_YES_NO_PAT = re.compile(r"^\s*(si|sì|no)\b.{0,20}$", re.I)


def safe_normalize(text: str) -> str | None:
    """Correzione STT: prima la memoria dei refusi noti (istantanea), poi Qwen.
    In entrambi i casi la proposta passa le guardie anti-danno. None se nulla
    di utilizzabile."""
    # 1) correzioni gia' apprese (confermate dall'utente in passato)
    # 'si'/'no' (event. con aggiunta breve: 'si va bene') NON si normalizzano:
    # sono risposte a una conferma e Qwen le trasformerebbe in comandi assurdi
    if _YES_NO_PAT.match(text):
        return None
    remembered = _learned_lookup(text)
    if remembered and _bare(text) != _bare(remembered):
        if _guards_ok(text, remembered):
            print(f"[learn] applicata correzione memorizzata: {text!r} -> {remembered!r}")
            return remembered
        print(f"[learn] scartata dalla guardia: {remembered!r}")
    # 2) Qwen come fallback
    cand = normalize_stt(text)
    if not cand or cand.lower() == text.strip().lower():
        return None
    if not _guards_ok(text, cand):
        print(f"[normalize] scartata dalle guardie: {cand!r}")
        return None
    return cand


def ollama_parse(text: str) -> dict | None:
    """Chiede al piccolo modello locale di tradurre il comando in JSON."""
    try:
        import urllib.request
        payload = json.dumps({
            "model": _llm_model(),
            "system": OLLAMA_SCHEMA,
            "prompt": text,
            "format": "json",
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 150},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        _q0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        _track("qwen_intent", time.time() - _q0)
        _track_tps("qwen_intent", data.get("eval_count", 0),
                   data.get("eval_duration", 0))
        spec = json.loads(data.get("response") or "{}")
        if not isinstance(spec, dict):
            return None
        # il modello 0.5B a volte usa {"command", "data"{...}} invece del formato piatto
        norm = {"action": str(spec.get("action") or spec.get("command") or "unknown")}
        nested = spec.get("data") if isinstance(spec.get("data"), dict) else {}
        for k in ("name", "content", "location", "app", "site", "query", "direction"):
            v = spec.get(k, nested.get(k))
            if isinstance(v, str) and v.strip():
                norm[k] = v.strip()
        return norm
    except Exception as exc:
        print(f"[ollama] errore: {exc}")
        return None

# ---------------------------------------------------------------------------
# Pipe CHAT (fallback agentic): le frasi che non sono comandi (domande,
# calcoli, curiosita') finivano in un secco "non ho capito"; ora Laya le
# indirizza ad 'unknown' e Qwen risponde davvero, in modalita' conversazionale
# con contesto della conversazione recente. I comandi veri non passano mai
# da qui: keyword/Laya li classificano prima e vanno alla pipe esecutiva.
# ---------------------------------------------------------------------------
CHAT_SCHEMA = (
    "Sei Ugo, l'assistente vocale offline del PC dell'utente. Rispondi in "
    "italiano, breve e parlato: 1-3 frasi, massimo 60 parole, niente elenchi "
    "puntati, niente markdown, niente emoji. Se ti chiedi un calcolo dai il "
    "risultato con una spiegazione essenziale. Se ti chiedi una definizione o "
    "una curiosita' rispondi in modo asciutto. Se la richiesta e' un'AZIONE da "
    "fare sul PC (aprire, chiudere, creare, volume, file) NON eseguirla e non "
    "inventare: di' che non e' tra le tue funzioni e ricorda brevemente che "
    "sai aprire app e siti, gestire file e volume. Non dire di essere un "
    "modello di linguaggio: sei Ugo."
)


def ollama_chat(text: str) -> str | None:
    """Risposta conversazionale del piccolo modello locale (domande generiche,
    calcoli, curiosita'): fallback agentic quando nessun comando combacia.
    Ritorna None se Ollama non risponde o la risposta non e' utilisabile."""
    try:
        import urllib.request
        with _log_lock:                       # ultime battute come contesto
            hist = [h for h in _history[-6:] if h.get("assistant")]
        ctx = "\n".join(f"Utente: {h.get('user', '')}\nUgo: {h['assistant']}"
                        for h in hist)
        payload = json.dumps({
            "model": _llm_model(),
            "system": CHAT_SCHEMA + (f"\nConversazione recente:\n{ctx}" if ctx else ""),
            "prompt": text,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.6, "num_predict": 160},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        _q0 = time.time()
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read().decode())
        _track("qwen_chat", time.time() - _q0)
        _track_tps("qwen_chat", data.get("eval_count", 0),
                   data.get("eval_duration", 0))
        out = " ".join((data.get("response") or "").split())
        # difese: vuoto o delirio lunghissimo -> meglio la risposta preimpostata
        if not out or len(out) > 600:
            return None
        return out
    except Exception as exc:
        print(f"[chat] errore: {exc}")
        return None

# parole chiave per il fallback testuale (sempre attivo, vince se trova un match)
KEYWORDS = [
    ("delete_folder", ("elimina la cartella", "elimina cartella", "cancella la cartella",
                       "cancella cartella", "rimuovi la cartella", "rimuovi cartella")),
    ("create_folder", ("crea una cartella", "crea cartella", "nuova cartella",
                       "creami una cartella", "creami cartella")),
    ("create_file", ("crea un file", "crea file", "nuovo file", "nuova nota",
                     "creami un file", "scrivi un file", "crea un documento di testo",
                     "crea un file di testo")),
    ("append_file", ("aggiungi al file", "aggiungi una riga", "scrivi nel file",
                     "aggiungi al file di testo")),
    ("delete_file", ("elimina il file", "cancella il file", "elimina il documento",
                     "cancella il documento")),
    ("read_file", ("leggi il file", "leggi il contenuto", "cosa c'e scritto nel file",
                   "leggi il documento", "cosa c'e scritto sul file")),
    ("note", ("appunta", "annota", "segna che", "prendi nota")),
    ("open_app", ("apri ", "lancia ", "avvia ")),
    ("close_app", ("chiudi ", "chiudere ", "termina ", "arresta ", "esci da ")),
    ("open_site", ("apri ", "vai su ", "vai a ", "portami su ", "cerca ", "ricerca ")),
    ("volume", ("volume", "muto", "mute")),
    ("time", ("che ore sono", "che ora e", "ora esatta", "orario")),
    ("date", ("che giorno", "data di oggi", "che data")),
    ("list_files", ("elenca i file", "mostra i file", "elenca i documenti",
                    "cosa c'e in", "lista file")),
    ("list_apps", ("quali app ho", "che app ho", "quali giochi ho", "che giochi ho",
                   "lista giochi", "lista app", "elenca i giochi", "elenca le app",
                   "giochi installati", "app installate", "applicazioni installate",
                   "mostra i giochi", "mostra le app", "cosa ho su", "cosa c'e su")),
]


def laya_intent(text: str):
    """Ritorna (label, confidence) usando Laya, oppure (None, 0)."""
    if laya_system is None:
        return None, 0.0
    try:
        res = laya_system.predict({"command": text}, LAYA_QUESTIONS)
        a = res["answers"]["intent"]
        return a["choice"], float(a["confidence"])
    except Exception as exc:
        print(f"[laya] errore: {exc}")
        return None, 0.0


def keyword_intent(text: str):
    t = " " + text.lower().strip() + " "
    # 1) i siti web hanno la precedenza: "apri youtube" e' un sito, non un'app.
    #    Cosi' "apri youtube" va al browser predefinito (es. Comet) e non viene
    #    scambiato per un eseguibile.
    for alias in SITE_ALIAS:
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return "open_site"
    # 2) il volume di UNA SINGOLA app ha la precedenza sul volume di sistema:
    #    'abbassa il volume di discord' come 'volume di comet al 60%' (senza
    #    verbo) regola il mixer per-app. 'volume del sistema/pc' resta master.
    if ("volume" in t or "audio" in t or "suono" in t) and \
            re.search(r"\b(?:volume|audio|suono)\s+(?:di|del|della|per)\s+[a-z]", t) and \
            not re.search(r"\b(?:sistema|computer|pc|dispositivo)\b", t):
        return "set_app_volume"
    # 3) poi le app, le cartelle e il resto
    for label, words in KEYWORDS:
        for w in words:
            if w in t:
                return label
    if "ore" in t or "ora" in t:
        return "time"
    return None


# Domande/conversazione: se la frase SEMBRA una domanda (interrogativo o '?')
# e non contiene keyword di comando, deve andare alla pipe conversazionale
# anche quando Laya la scambia per un comando ('quanto fa 1+1' -> volume, 0.86:
# il classificatore e' affidabile sui comandi, poco sulle frasi fuori dominio).
_QUESTION_RE = re.compile(
    r"^(?:quanto|quant'?|quale|qual|qual'?|come|perch[e']|chi|dove|quando|dimmi|"
    r"racconta|spiega|conosci|sai(?:\s+dirmi)?)\b|\?\s*$", re.IGNORECASE)


def detect_intent(text: str):
    """Laya propone, le parole chiave confermano. Ritorna (label, source)."""
    kw = keyword_intent(text)
    if kw:
        return kw, "keyword"
    if _QUESTION_RE.search((text or "").strip()):
        # domanda senza verbi-comando: pipe conversazionale (chat LLM)
        return "unknown", "question"
    label, conf = laya_intent(text)
    if label and label != "unknown" and conf >= CONF_THRESHOLD:
        return label, f"laya({conf:.2f})"
    return "unknown", "none"


# ---------------------------------------------------------------------------
# Esecuzione comandi reali
# ---------------------------------------------------------------------------
def extract_folder_name(text: str) -> str:
    """Cerca il nome della cartella: 'chiamata X', 'nome X', altrimenti ultima parola utile."""
    t = text.lower()
    m = re.search(r"(?:chiamat[oa]|di nome|nome)\s+(?:la\s+|la\s+nuova\s+|una\s+)?([a-z0-9 _\-]+)", t)
    if m:
        name = m.group(1).strip()
    else:
        words = [w for w in re.findall(r"[a-z0-9_\-]+", t) if w not in PLACE_WORDS]
        name = words[-1] if words else ""
    name = re.sub(r"\b(sul|sulla|nel|nella|in|su|desktop|scrivania|documenti|download)\b.*$", "", name).strip()
    name = name.strip(" .,!?")
    if name:
        name = name[0].upper() + name[1:]  # "prova" -> "Prova"
    return name


def extract_location(text: str) -> Path:
    t = text.lower()
    for key, path in FOLDER_MAP.items():
        if key in t:
            return path
    return DEFAULT_LOCATION


def resolve_folder(text: str, name: str) -> Path | None:
    """Trova la cartella citata cercando in Desktop, Documenti e Download."""
    if not name:
        return None
    loc = extract_location(text)
    for base in (HOME, HOME / "Desktop", HOME / "Documents", HOME / "Downloads"):
        if base.is_dir() and base.name.lower() == name:
            return base
    for base in {loc, HOME}:
        p = base / name
        if p.is_dir():
            return p
    return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80].strip(" .")


def execute_spec(spec: dict, text: str) -> str:
    """Esegue la specifica JSON prodotta dal piccolo modello (file e altro)."""
    tl = text.lower()
    action = str(spec.get("action") or "unknown")
    loc = FOLDER_MAP.get(str(spec.get("location") or "").lower())
    if loc is None:
        loc = extract_location(text)
    name = sanitize_filename(str(spec.get("name") or ""))
    content = str(spec.get("content") or "").strip()

    # fallback deterministici: il modello 0.5B a volte perde nome o contenuto
    if action in FILE_INTENTS and not name:
        m = (re.search(r"\b(?:chiamat[oa]|di nome|nome)\s+(?:un\s+|una\s+|il\s+|la\s+)?"
                       r"([a-z0-9_. \-]+?)(?:\s+(?:sul|nel|nella|in|con|dentro)\b.*$|$)", tl)
             or re.search(r"\b(?:file|documento|nota)\s+([a-z0-9_.\-]+)\b"
                          r"(?!\s+(?:di\b|testo\b))", tl))
        if m:
            name = sanitize_filename(m.group(1))
    if action in ("create_file", "append_file"):
        m = re.search(r"\bcon\s+(?:dentro|scritto|il testo)\s+(.+?)\s*$", tl)
        if m:
            content = re.sub(r"\s+(?:sul|nel|nella|in)\s+(?:desktop|documenti|downloads|home)\s*$",
                             "", m.group(1)).strip(" .")

    if action in FILE_INTENTS and name and "." not in name:
        name += ".txt"  # di default i file creati a voce sono di testo

    if action == "create_file":
        if not name:
            return "Come vuoi chiamare il file?"
        target = loc / name
        if target.exists():
            return f"Il file {name} esiste gia' in {loc}."
        target.write_text(content, encoding="utf-8")
        extra = f" con scritto: {content[:80]}" if content else " vuoto"
        return f"File {name} creato in {loc}{extra}."

    if action == "append_file":
        if not name:
            return "Su quale file devo scrivere?"
        if not content:
            return "Cosa devo scrivere nel file?"
        target = loc / name
        if target.exists():
            body = target.read_text(encoding="utf-8", errors="ignore")
            if body and not body.endswith("\n"):
                body += "\n"  # la nuova riga non deve incollarsi alla precedente
            target.write_text(body + content + "\n", encoding="utf-8")
            return f"Aggiunta una riga al file {name}."
        target.write_text(content + "\n", encoding="utf-8")
        return f"Il file {name} non c'era: l'ho creato in {loc} con la riga dentro."

    if action == "delete_file":
        if not name:
            return "Quale file devo eliminare?"
        target = loc / name
        if not target.exists():
            return f"Non trovo il file {name} in {loc}."
        send2trash.send2trash(str(target))
        return f"Il file {name} e' nel cestino."

    if action == "read_file":
        if not name:
            return "Quale file devo leggere?"
        target = loc / name
        if not target.exists():
            return f"Non trovo il file {name} in {loc}."
        try:
            body = target.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            return f"Non riesco a leggere il file {name}."
        if not body:
            return f"Il file {name} e' vuoto."
        preview = body[:160].replace("\n", ". ")
        more = " e altro" if len(body) > 160 else ""
        return f"Il file {name} contiene: {preview}{more}."

    # il modello puo' anche ri-instradare verso i comandi gia' esistenti
    if action == "open_app" and spec.get("app"):
        return run_command(f"apri {spec['app']}", "open_app")
    if action == "open_site" and spec.get("site"):
        return run_command(f"apri {spec['site']}", "open_site")
    if action == "search_web" and spec.get("query"):
        site = "youtube" if "youtube" in str(spec.get("site") or "") else "google"
        return run_command(f"cerca {spec['query']} su {site}", "open_site")
    if action == "set_app_volume" and spec.get("app"):
        try:
            level = max(0, min(100, int(str(spec.get("level") or "").strip().rstrip("%"))))
        except ValueError:
            level = None
        if level is None:  # senza livello: relativo dal verbo nella frase
            mode = "down" if any(w in tl for w in ("abbassa", "diminuisci", "riduci", "azzer")) else "up"
            return _set_app_volume(str(spec["app"]), mode, 10)
        return _set_app_volume(str(spec["app"]), "abs", level)

    # la spec non contiene nulla di eseguibile (o l'azione era solo un nome):
    # NON rimandare la frase al fallback preimpostato di run_command — e'
    # qui che 'quanto fa 1+1' (spec = {action: set_app_volume} senza campi)
    # moriva nel 'non ho capito' senza mai raggiungere la pipe conversazionale
    chat = ollama_chat(text)
    if chat:
        return chat
    return ("Non ho capito il comando. Posso creare o eliminare cartelle e file di testo, "
            "aprire app e siti, cercare su YouTube o Google, darti ora e data, "
            "regolare il volume o elencare i file.")


def _extract_launcher(text: str) -> str | None:
    """Dalla frase individua il launcher citato: steam, epic, gog, tutti..."""
    t = text.lower()
    if re.search(r"\bepic\b", t):
        return "epic"
    if re.search(r"\bgog\b", t):
        return "gog"
    if re.search(r"\bsteam\b", t):
        return "steam"
    for m in ("riot", "league of legends", "ubisoft", "battle.net", "battlenet",
              "origin", "ea app", "ea games"):
        if m in t:
            return "launcher"
    return None


def _fmt_game_list(gl: list) -> str:
    """Lista compatta leggibile ad alta voce: max 12 titoli + contatore."""
    names = [g["name"] for g in gl]
    if len(names) <= 12:
        return ", ".join(names)
    return ", ".join(names[:12]) + f"... e altri {len(names) - 12}."


def _handle_list_apps(text: str) -> str:
    """'quali giochi ho su steam' / 'quali app ho installato': prepara la lista
    per la modale della UI (in _last_items) e una risposta parlata."""
    global _last_items
    launcher = _extract_launcher(text)
    if launcher:
        gl = games.by_launcher(launcher)
        label = games.LAUNCHER_LABEL.get(launcher, launcher)
        _last_items = [{"title": g["name"], "subtitle": label, "kind": g["launcher"]} for g in gl]
        if not gl:
            return f"Non mi risulta nessun gioco installato da {label}."
        noun = "launcher" if launcher == "launcher" else "gioco" if len(gl) == 1 else "giochi"
        verb = "Hai" if launcher != "launcher" else "Ho trovato"
        return f"{verb} {len(gl)} {noun} da {label}: {_fmt_game_list(gl)}"
    # senza launcher citato: tutto quello che ho trovato
    gl = games.get_games()
    apps = appindex.get_apps()
    _last_items = ([{"title": g["name"], "subtitle": games.LAUNCHER_LABEL.get(g["launcher"], g["launcher"]),
                     "kind": g["launcher"]} for g in gl] +
                    [{"title": a["name"], "subtitle": "applicazione", "kind": a["kind"]} for a in apps])
    return (f"Hai {len(apps)} applicazioni e {len(gl)} giochi indicizzati: "
            "ti apro l'elenco completo a schermo.")


# parole-numero italiano per le percentuali del volume ("al settanta", "del quindici")
_IT_NUMBERS = {
    "zero": 0, "dieci": 10, "venti": 20, "trenta": 30, "quaranta": 40,
    "cinquanta": 50, "sessanta": 60, "settanta": 70, "ottanta": 80,
    "novanta": 90, "cento": 100,
}


def _parse_volume(t: str):
    """Estrae (livello, modo) dal comando volume: livello 0-100 o None,
    modo in {'abs','up','down'}. Capisce: 'al 30', '30%', 'al settanta',
    'a meta', 'al massimo', 'al minimo', 'alza del 20', 'abbassa di 5'."""
    m = re.search(r"(\d{1,3})\s*%", t) or re.search(
        r"\b(al|alla|a|del|di)\s+(\d{1,3})\b", t)
    if not m:
        m = re.search(r"\b(\d{1,3})\b", t)  # 'volume 30'
    if m:
        n = int(m.group(2) if m.lastindex == 2 else m.group(1))
        prefix = m.group(1) if m.lastindex == 2 else ""
        if 0 <= n <= 100:
            verb = any(w in t for w in ("alza", "aumenta", "abbassa",
                                        "diminuisci", "riduci"))
            if prefix in ("del", "di") or (not prefix and verb):
                # 'alza del 20', 'abbassa di 5', 'alza il volume 20'
                up = any(w in t for w in ("alza", "aumenta"))
                return n, "up" if up else "down"
            return n, "abs"
    for word, val in _IT_NUMBERS.items():
        if re.search(rf"\b(?:al|a|alla|del|di)?\s*{word}\b", t):
            return val, "abs"
    if "massimo" in t or "al max" in t:
        return 100, "abs"
    if "minimo" in t or "al min" in t:
        return 0, "abs"
    if "meta" in t or "metà" in t or "mezzo" in t:
        return 50, "abs"
    m = re.search(r"(?:alza|aumenta|abbassa|diminuisci|riduci)[^\d]{0,20}(\d{1,3})", t)
    if m and 0 <= int(m.group(1)) <= 100:
        up = any(w in t for w in ("alza", "aumenta"))
        return int(m.group(1)), "up" if up else "down"
    return None, ("up" if ("alza" in t or "aumenta" in t or "su" in t)
                  else "down")


def _endpoint_volume():
    """Puntatore IAudioEndpointVolume, compatibile con tutte le versioni di pycaw:
    le recenti espongono dev.EndpointVolume gia' attivato, le vecchie richiedono
    dev.Activate(...)."""
    import comtypes
    from ctypes import cast, POINTER
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    comtypes.CoInitialize()
    dev = AudioUtilities.GetSpeakers()
    if getattr(dev, "EndpointVolume", None):
        return cast(dev.EndpointVolume, POINTER(IAudioEndpointVolume))
    iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


# ---------------------------------------------------------------------------
# Volume PER-APPLICAZIONE: regola la sessione audio del singolo processo
# (l'icona "Mixer volume" di Windows), NON il volume master del sistema.
# ---------------------------------------------------------------------------
def _audio_sessions() -> list:
    """Sessioni audio attive: [(processo_minuscolo, volume, sessione), ...]"""
    if not pu.IS_WINDOWS:
        return []
    try:
        from pycaw.pycaw import AudioUtilities
        import comtypes
        comtypes.CoInitialize()
        out = []
        for s in AudioUtilities.GetAllSessions():
            try:
                if s.Process is None or s.SimpleAudioVolume is None:
                    continue
                name = (s.Process.name() or "").lower().removesuffix(".exe")
                if name:
                    out.append((name, s.SimpleAudioVolume, s))
            except Exception:
                continue
        return out
    except Exception as exc:
        print(f"[volume-app] errore pycaw: {exc}")
        return []


# token che non fanno parte del nome dell'app nel comando volume
_APPVOL_NOISE = re.compile(
    r"\b(?:abbassa|alza|aumenta|diminuisci|riduci|imposta|metti|porta|regola|"
    r"azzer\w*|silenz\w*|tira)\b|\b(?:il|lo|la|l'|un|una|di|del|della|"
    r"dei|delle|al|alla|a|su|volume|audio|suono|percento|per\s?cento)\b|"
    r"\d+\s*%?|\b(?:al|a|del|di|su)\s+\d+\b|\b(?:settanta|trenta|venti|dieci|"
    r"quaranta|cinquanta|sessanta|ottanta|novanta|cento|massimo|minimo|meta)\b")


def _appvol_app_name(t: str) -> str:
    """Estrae il nome dell'app dal comando 'abbassa il volume di discord al 30%':
    tutto quello che sta tra il verbo e il primo 'di/a' o la percentuale."""
    m = re.search(
        r"(?:(?:abbassa|alza|aumenta|diminuisci|riduci|imposta|metti|regola|"
        r"azzer\w*|silenz\w*|tira)\b.*?)?"
        r"\b(?:volume|audio|suono)\s*(?:di|del|della|per)\s+(.+?)"
        r"\s*(?:\bal\b|\ba\b|alla|\d|%|$)", t)
    if not m:
        return ""
    name = _APPVOL_NOISE.sub(" ", m.group(1))
    return re.sub(r"\s+", " ", name).strip(" ?!.,")


def _parse_app_volume(t: str):
    """Percentuale dal comando volume-app: ('abs', 30) | ('up', 10) | ('down', 10).
    Diverso dal volume master: qui 'al 30' / '30%' e' ASSOLUTO anche con
    'abbassa' davanti ('abbassa il volume di discord al 30%' = mettilo A 30),
    mentre 'del/di 20' e' relativo ('abbassa ... del 20' = togli 20)."""
    if re.search(r"\bazzer\w*", t):
        return "abs", 0
    m = (re.search(r"\b(?:al|alla|a)\s+(\d{1,3})\b", t)
         or re.search(r"(\d{1,3})\s*%", t)
         or re.search(r"\b(?:volume|audio|suono)\s+(\d{1,3})\b", t))
    if m and 0 <= int(m.group(1)) <= 100:
        return "abs", int(m.group(1))
    m = re.search(r"\b(?:del|di)\s+(\d{1,3})\b", t)
    if m and 0 <= int(m.group(1)) <= 100:  # 'abbassa del 20' -> relativo
        up = any(w in t for w in ("alza", "aumenta"))
        return ("up" if up else "down"), int(m.group(1))
    for word, val in _IT_NUMBERS.items():
        if re.search(rf"\b(?:al|a|alla)\s+{word}\b", t):
            return "abs", val
    if "massimo" in t or "al max" in t:
        return "abs", 100
    if "minimo" in t or "al min" in t:
        return "abs", 0
    if "meta" in t or "met\u00e0" in t or "mezzo" in t:
        return "abs", 50
    return None


def _resolve_audio_process(name: str) -> str | None:
    """Trova il processo della sessione audio che combacia meglio col nome detto
    ('discord' -> 'discord', 'league of legends' -> 'leagueclient', ...)."""
    if not name:
        return None
    from difflib import get_close_matches
    sessions = _audio_sessions()
    procs = sorted({p for p, _, _ in sessions})
    if not procs:
        return None
    n = name.lower().strip()
    # 1) match esatto o sottostringa ('discord' in 'discordptt' ecc.)
    for p in procs:
        if n == p or n in p or p in n:
            return p
    # 2) fuzzy
    m = get_close_matches(n, procs, n=1, cutoff=0.6)
    return m[0] if m else None


def _set_app_volume(app_name: str, mode: str, level: int) -> str:
    """Imposta/relativa il volume della sessione audio di UNA app.
    mode: 'abs' (imposta al livello), 'up'/'down' (relativo)."""
    if not pu.IS_WINDOWS:
        return "Il volume per singola applicazione al momento funziona solo su Windows."
    proc = _resolve_audio_process(app_name)
    if not proc:
        sess = [p for p, _, _ in _audio_sessions()]
        return (f"Non trovo nessuna app attiva con l'audio acceso che si chiami "
                f"'{app_name}'. App con audio in questo momento: "
                f"{', '.join(sess) if sess else 'nessuna'}.")
    sessions = _audio_sessions()
    vol = next((v for p, v, _ in sessions if p == proc), None)
    if vol is None:
        return f"Non riesco ad accedere al volume di {proc}."
    cur = int(round(vol.GetMasterVolume() * 100))
    if mode == "abs":
        new = max(0, min(100, level))
    elif mode == "up":
        new = min(100, cur + level)
    else:
        new = max(0, cur - level)
    vol.SetMasterVolume(new / 100.0, None)
    got = int(round(vol.GetMasterVolume() * 100))
    if abs(got - new) > 3:
        return f"Non sono riuscito a regolare il volume di {proc} al {new} per cento."
    if got == 0:
        return f"Volume di {proc} azzerato."
    return f"Volume di {proc} portato al {got} per cento."


def _get_app_volume(app_name: str) -> str:
    """Legge il volume attuale della sessione audio di UNA app
    ('volume di discord?' senza verbi ne' numeri)."""
    if not pu.IS_WINDOWS:
        return "Il volume per singola applicazione al momento funziona solo su Windows."
    proc = _resolve_audio_process(app_name)
    if not proc:
        sess = sorted({p for p, _, _ in _audio_sessions()})
        return (f"Non trovo nessuna app attiva con l'audio acceso che si chiami "
                f"'{app_name}'. App con audio in questo momento: "
                f"{', '.join(sess) if sess else 'nessuna'}.")
    vol = next((v for p, v, _ in _audio_sessions() if p == proc), None)
    if vol is None:
        return f"Non riesco a leggere il volume di {proc}."
    cur = int(round(vol.GetMasterVolume() * 100))
    return (f"{proc} e' muto." if cur == 0
            else f"Volume di {proc} al {cur} per cento.")


# processi che Ugo si rifiuta di chiudere (sistema o se stesso)
_CLOSE_PROTECTED = {"explorer", "winlogon", "csrss", "dwm", "system", "idle",
                    "python", "pythonw", "ollama", "audiodg", "svchost", "services"}


def _running_processes() -> list:
    """Nomi immagine (senza .exe) dei processi attivi, minuscoli e deduplicati."""
    if not pu.IS_WINDOWS:
        # mac / Linux: psutil (niente tasklist.exe)
        try:
            import psutil
            return sorted({p.info["name"].lower().removesuffix(".exe")
                           for p in psutil.process_iter(["name"]) if p.info["name"]})
        except Exception:
            return []
    try:
        r = _run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        out = []
        for line in r.stdout.splitlines():
            if line.startswith('"'):
                name = line.split('","')[0].strip('"').lower()
                if name.endswith(".exe"):
                    out.append(name[:-4])
        return list(dict.fromkeys(out))  # dedup mantenendo l'ordine
    except Exception:
        return []


def _still_running(img: str) -> bool:
    """True se esiste ancora un processo con quel nome immagine."""
    if not pu.IS_WINDOWS:
        try:
            import psutil
            base = img.lower().removesuffix(".exe")
            return any(p.info["name"].lower().removesuffix(".exe") == base
                       for p in psutil.process_iter(["name"]) if p.info["name"])
        except Exception:
            return False
    try:
        r = _run(["tasklist", "/FI", f"IMAGENAME eq {img}"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW if pu.IS_WINDOWS else 0)
        return img.lower() in (r.stdout or "").lower()
    except Exception:
        return False


def _kill_by_image(base: str, force: bool = False, display: str | None = None) -> str:
    """Chiude i processi con quel nome immagine: prima tenta la chiusura
    'educata' (l'app salva e esce), poi, se serve, la forza. `display` e'
    il nome da usare nelle frasi di risposta (es. 'la calcolatrice')."""
    img = re.sub(r"[^A-Za-z0-9_.-]", "", base)  # niente injection negli argomenti
    if not img:
        return "Nome processo non valido."
    if not img.lower().endswith(".exe"):
        img += ".exe"
    if img.lower()[:-4] in _CLOSE_PROTECTED:
        return "Questo processo di sistema non lo chiudo per sicurezza."
    nome = display or base
    if not pu.IS_WINDOWS:
        # mac / Linux: pkill prima educato (SIGTERM), poi forzato (SIGKILL)
        try:
            r = _run(["pkill", "-f" if force else "-x", base],
                               capture_output=True, text=True, timeout=15)
            return (f"Ho chiuso {nome}." if r.returncode == 0
                    else f"Non vedo {nome} tra i programmi aperti.")
        except FileNotFoundError:
            return "Comando pkill non disponibile su questo sistema."
        except Exception as exc:
            return f"Errore chiudendo {nome}: {exc}"
    flags = subprocess.CREATE_NO_WINDOW if pu.IS_WINDOWS else 0
    try:
        if force:
            r = _run(["taskkill", "/IM", img, "/F"], capture_output=True,
                               text=True, timeout=15, creationflags=flags)
            return (f"Ho chiuso {nome} (forzato)." if r.returncode == 0
                    else f"Non vedo {nome} tra i programmi aperti.")
        r = _run(["taskkill", "/IM", img], capture_output=True,
                           text=True, timeout=15, creationflags=flags)
        if r.returncode == 0:
            # le app UWP possono ignorare il segnale 'educato': verifico davvero
            time.sleep(1.0)
            if not _still_running(img):
                return f"Ho chiuso {nome}."
            print(f"[close_app] {img} resisteva alla chiusura educata: forzo")
        r2 = _run(["taskkill", "/IM", img, "/F"], capture_output=True,
                            text=True, timeout=15, creationflags=flags)
        if r2.returncode == 0:
            time.sleep(0.5)
            if not _still_running(img):
                return f"{nome} non si chiudeva: chiusura forzata eseguita."
            return f"Non riesco a chiudere {nome}."
        return f"Non vedo {nome} tra i programmi aperti."
    except FileNotFoundError:
        return "La chiusura delle app e' disponibile solo su Windows."
    except Exception as exc:
        return f"Errore chiudendo {nome}: {exc}"


def _alias_exe(alias: str, cmd):
    """Se l'alias di APP_ALIAS punta a un .exe, ritorna il nome del processo."""
    cand = cmd[0] if isinstance(cmd, list) and cmd else cmd
    if isinstance(cand, str) and cand.lower().endswith(".exe"):
        return os.path.basename(cand)
    return None


def _close_app(t: str) -> str:
    """Chiude un'applicazione: alias noti -> confronto con i processi realmente
    attivi (l'app deve essere aperta) -> voci della libreria -> fuzzy.
    Restituisce la frase da dire."""
    rest = re.sub(r"^(chiudi|chiudere|termina|arresta|esci da)\s+"
                  r"(il\s+|la\s+|lo\s+|l'|un\s+|una\s+)?", "", t).strip(" .!?")
    force = bool(re.search(r"\b(forza|forzatamente|subito|ammazza[rl]?)\b", rest))
    rest = re.sub(r"\b(forza|forzatamente|subito|ammazza[rl]?)\b", "", rest).strip(" .!?")
    if not rest:
        return "Quale applicazione devo chiudere?"
    norm = appindex._norm(rest)
    running = _running_processes()

    alias_name = None
    # 1) alias noti che puntano a un eseguibile (calcolatrice -> calc/CalculatorApp)
    for alias, cmd in APP_ALIAS.items():
        if alias in rest or rest.startswith(alias):
            exe = _alias_exe(alias, cmd)
            if not exe:
                continue
            stem = exe[:-4].lower()
            if stem in running:
                return _kill_by_image(stem, force, display=alias)
            # l'exe storico non gira: cerco il nome dell'alias tra i processi reali
            hit = difflib.get_close_matches(alias, running, n=1, cutoff=0.6)
            if hit:
                print(f"[close_app] alias {alias!r}: {stem!r} non attivo, uso {hit[0]!r}")
                return _kill_by_image(hit[0], force, display=alias)
            alias_name = alias   # es. il Blocco note di Windows 11 e' un'app Store:
            break                # prosegue con processi reali e libreria sotto

    # 2) il nome detto combacia con un processo realmente attivo:
    #    esatto -> sottostringa ('spotify' in Spotify) -> fuzzy ('codestraits')
    proc = norm.split()[-1] if norm.split() else ""
    if proc and running:
        cand = [p for p in running if p == proc] or \
               [p for p in running if proc in p] or \
               difflib.get_close_matches(proc, running, n=1, cutoff=0.72)
        if cand and cand[0] not in _CLOSE_PROTECTED:
            return _kill_by_image(cand[0], force, display=cand[0].capitalize())

    # 3) nome esatto nella libreria app: dal target ricavo il processo;
    #    per le app Store provo anche la coda del package ('WindowsNotepad'
    #    contiene 'notepad', che e' il nome del processo reale)
    candidates = []
    for a in appindex.get_apps():
        if appindex._norm(a["name"]) == norm:
            tgt = a.get("target") or ""
            if a["kind"] == "appx" and "!" in tgt:
                candidates.append(tgt.rsplit("!", 1)[-1])
                candidates.append(tgt.rsplit("!", 1)[0].split(".")[-1])
            elif tgt.lower().endswith(".exe"):
                candidates.append(os.path.splitext(os.path.basename(tgt))[0])
    for cand in candidates:
        c = cand.lower()
        if c in running:
            return _kill_by_image(c, force, display=rest)
        hit = next((p for p in running
                    if (c in p or p in c) and p not in _CLOSE_PROTECTED
                    and min(len(p), len(c)) >= 5), None)
        if hit:
            print(f"[close_app] package {cand!r} -> processo {hit!r}")
            return _kill_by_image(hit, force, display=rest)
    if candidates:
        return f"{rest} non e' aperta al momento."

    # 4) fuzzy sul nome della libreria ('visual studio cod' -> Visual Studio Code)
    close = difflib.get_close_matches(
        norm, [appindex._norm(a["name"]) for a in appindex.get_apps()], n=1, cutoff=0.75)
    if close:
        match = next(a for a in appindex.get_apps() if appindex._norm(a["name"]) == close[0])
        tgt = match.get("target") or ""
        if match["kind"] == "appx" and "!" in tgt:
            proc = tgt.rsplit("!", 1)[-1]
        elif tgt.lower().endswith(".exe"):
            proc = os.path.splitext(os.path.basename(tgt))[0]
        else:
            proc = ""
        print(f"[close_app] fuzzy: {rest!r} -> {match['name']!r} ({proc})")
        if proc and proc.lower() in running:
            return _kill_by_image(proc, force)
        if proc:
            return f"{match['name']} non e' aperta al momento."

    if alias_name:
        return f"Non vedo {alias_name} tra i programmi aperti."
    return f"Non trovo nessuna applicazione chiamata {rest}."


def run_command(text: str, intent: str) -> str:
    """Esegue il comando e ritorna la frase da dire alla voce."""
    t = text.lower()

    # 'quali app/giochi ho' -> lista indicizzata, mostrata in modale dalla UI
    if intent == "list_apps":
        return _handle_list_apps(text)

    # Note vocali: "appunta che ..." -> appende in Note.txt sul desktop
    if intent == "note":
        m = re.match(r"^\s*(?:appunta|annota|segna(?:\s+che)?|prendi nota(?:\s+di)?)\b[,: ]*(.+?)\s*$", t)
        content = (m.group(1) if m else text).strip()
        target = DEFAULT_LOCATION / "Note.txt"
        body = ""
        if target.exists():
            body = target.read_text(encoding="utf-8", errors="ignore")
            if body and not body.endswith("\n"):
                body += "\n"
        target.write_text(body + content + "\n", encoding="utf-8")
        return f"Appuntato in Note.txt: {content[:80]}."

    # I file passano dal piccolo modello: estrae nome, contenuto e posizione.
    if intent in FILE_INTENTS:
        spec = ollama_parse(text)
        if spec is None:
            return "Per gestire i file mi serve Ollama ma non risponde: e' avviato?"
        spec["action"] = intent  # la categoria e' gia' certa dalle parole chiave
        return execute_spec(spec, text)

    if intent == "create_folder":
        name = extract_folder_name(text)
        if not name:
            return "Come vuoi che si chiami la cartella?"
        loc = extract_location(text)
        target = loc / name
        if target.exists():
            return f"La cartella {name} esiste gia' sul {loc.name.lower()}."
        target.mkdir(parents=True, exist_ok=False)
        return f"Fatto. Ho creato la cartella {name} in {loc}."

    if intent == "delete_folder":
        name = extract_folder_name(text)
        target = resolve_folder(text, name)
        if target is None:
            return f"Non ho trovato nessuna cartella chiamata {name or 'cosi'}."
        send2trash.send2trash(str(target))  # nel cestino, recuperabile
        return f"Ho spostato la cartella {target.name} nel cestino."

    if intent == "close_app":
        return _close_app(t)

    if intent == "open_app":
        rest = re.sub(r"^(apri|lancia|avvia)\s+(il\s+|la\s+|lo\s+|l'|un\s+|una\s+)?", "", t).strip(" .!?")
        for alias, cmd in APP_ALIAS.items():
            if alias in rest or rest.startswith(alias):
                try:
                    if isinstance(cmd, list):
                        _popen(cmd, shell=True)
                    else:
                        os.startfile(cmd)  # noqa: S606 - intenzionale, comando utente
                    return f"Sto aprendo {alias}."
                except Exception:
                    continue
        # 1) libreria indicizzata: .lnk, app Store/AppX e portabili
        # 0) alias confermato in passato ('stimolo' -> Steam): apre direttamente
        remembered_app = _learned_app_lookup(rest)
        if remembered_app:
            try:
                appindex.launch(next(a for a in appindex.get_apps()
                                     if a["name"] == remembered_app))
                print(f"[open_app] alias memorizzato: {rest!r} -> {remembered_app!r}")
                return f"Sto aprendo {remembered_app}."
            except Exception:
                pass  # l'app non esiste piu': ricado nella ricerca normale
        # i refusi appresi valgono anche a livello NOME: 'apri lo spotifi'
        # diventa 'apri lo Spotify' prima ancora di cercare nell'indice
        rest = _learned_token_fix(rest)
        hits = appindex.search(rest, limit=3)
        if not hits and rest:
            # nessun hit: i "fratelli" simili (Steam/SteamVR, Code/Code - Insiders)
            # diventano un menu a scelta numerata invece del primo del pattern
            close_all = difflib.get_close_matches(
                appindex._norm(rest),
                [appindex._norm(a["name"]) for a in appindex.get_apps()],
                n=3, cutoff=0.55)
            if len(close_all) >= 2:
                by_n = {appindex._norm(a["name"]): a for a in appindex.get_apps()}
                menu = [by_n[c]["name"] for c in close_all if c in by_n][:3]
                with _log_lock:
                    _pending["app"] = None
                    _pending["raw_said"] = rest
                    _pending["text"] = None
                    _pending["ts"] = time.time()
                    _pending_choices["menu"] = menu
                opts = " o ".join(f"{i + 1}) {n}" for i, n in enumerate(menu))
                return (f'Non ho nessuna app chiamata {rest}. Vuoi {opts}? '
                        'Dimmi primo, secondo o terzo.')
        if not hits:
            # fallback fuzzy: il nome era storpiato ('spotrifyt' -> 'Spotify')
            close = difflib.get_close_matches(
                appindex._norm(rest),
                [appindex._norm(a["name"]) for a in appindex.get_apps()],
                n=1, cutoff=0.72)
            if close:
                hits = [a for a in appindex.get_apps()
                        if appindex._norm(a["name"]) == close[0]][:1]
                print(f"[open_app] fuzzy: {rest!r} -> {hits[0]['name']!r}")
        if not hits and rest:
            # ultimo grado: chiede a Qwen tra le app installate e CONFERMA
            # prima di avviare ('stimolo' -> 'Intendavi Steam?')
            sugg = qwen_app_suggest(rest)
            if sugg and "qwen_menu" in sugg:
                menu = sugg["qwen_menu"][:3]
                with _log_lock:
                    _pending["app"] = None
                    _pending["raw_said"] = rest
                    _pending["text"] = None
                    _pending["ts"] = time.time()
                    _pending_choices["menu"] = menu
                opts = " o ".join(f"{i + 1}) {n}" for i, n in enumerate(menu))
                return (f'Quale delle due intendevi per {rest}: {opts}? '
                        'Dimmi primo o secondo.')
            if sugg:
                with _log_lock:
                    _pending["app"] = sugg["name"]
                    _pending["raw_said"] = rest  # per imparare l'alias se confermi
                    _pending["text"] = None
                    _pending["ts"] = time.time()
                print(f"[open_app] qwen suggerisce {rest!r} -> {sugg['name']!r}, chiedo conferma")
                return (f'Non ho nessuna app chiamata {rest}. '
                        f'Intendavi {sugg["name"]}? Rispondi sì o no.')
        if hits:
            app = hits[0]
            try:
                appindex.launch(app)
                extra = " (Microsoft Store)" if app["kind"] == "appx" else ""
                return f"Sto aprendo {app['name']}{extra}."
            except Exception as exc:
                print(f"[open_app] launch {app} fallito: {exc}")
        # 2) vecchio percorso: scorciatoie Start via cache .lnk (solo Windows)
        lnk = _find_shortcut(rest) if pu.IS_WINDOWS else None
        if lnk is not None:
            try:
                os.startfile(str(lnk))  # noqa: S606
                return f"Sto aprendo {lnk.stem}."
            except Exception as exc:
                print(f"[open_app] startfile({lnk.name}) fallito: {exc}")
                # fallback: prova l'eseguibile dichiarato nel collegamento
                try:
                    import win32com.client  # type: ignore
                    sh = win32com.client.Dispatch("WScript.Shell")
                    target = sh.CreateShortCut(str(lnk)).Targetpath
                    if target:
                        _popen([target], shell=True)
                        return f"Sto aprendo {lnk.stem}."
                except Exception:
                    pass
        # 3) eseguibile nel PATH (notepad, calc...)
        exe = shutil.which(rest) or shutil.which(rest + ".exe")
        if exe:
            _popen([exe])
            return f"Sto aprendo {rest}."
        # 4) ultimo tentativo: apertura col programma predefinito del sistema
        try:
            pu.open_path(rest)  # noqa: S606
            return f"Sto aprendo {rest}."
        except Exception:
            return (f"Non trovo nessuna app chiamata {rest}. "
                    "Controlla che sia installata e che il nome sia giusto.")

    if intent == "open_site":
        # -- ricerche esplicite su YouTube / Google, in tutte le formulazioni --
        def web_search(site: str, query: str) -> str:
            q = urllib.parse.quote_plus(query)
            if site == "youtube":
                url = f"https://www.youtube.com/results?search_query={q}"
            else:
                url = f"https://www.google.com/search?q={q}"
            webbrowser.open(url)  # browser predefinito su TUTTI i sistemi operativi
            return f"Cerco {query} su {site.capitalize()}."

        # 1) "cerca X su youtube/google" (\b per non far combaciare "cerca" dentro "ricerca")
        m = re.search(r"\bcerca\s+(?:su\s+)?(.+?)\s+su\s+(youtube|google)\b", t)
        if m:
            return web_search(m.group(2), m.group(1))
        # 2) "fai/apri una ricerca su youtube (di X)"
        m = re.search(r"(?:apri|fai|vai|portami|mostra)\s+(?:una\s+|la\s+)?ricerca\s+"
                      r"su\s+(youtube|google)(?:\s+(?:di|per|su)\s+(.+?)\s*)?$", t)
        if m and m.group(2):
            return web_search(m.group(1), m.group(2))
        # 3) "su youtube cerca X"
        m = re.search(r"su\s+(youtube|google)\s+(?:cerca|ricerca)\s+(.+?)\s*$", t)
        if m:
            return web_search(m.group(1), m.group(2))
        # 4) "apri (la ricerca di) X su youtube/google"
        m = re.search(r"(?:apri|fai|vai|portami|mostra)\s+(?:la\s+|una\s+|il\s+)?"
                      r"(?:ricerca\s+)?(?:di\s+|per\s+|la\s+)?"
                      r"((?!ricerca\b|cerca\b).+?)\s+su\s+(youtube|google)\b", t)
        if m:
            return web_search(m.group(2), m.group(1))
        # 5) "cerca X" senza sito -> Google
        m = re.search(r"\b(?:cerca|ricerca)\s+(?:di\s+|la\s+|per\s+)?(.+?)\s*$", t)
        if m:
            return web_search("google", m.group(1))
        for alias, url in SITE_ALIAS.items():
            if re.search(rf"\b{re.escape(alias)}\b", t):
                # webbrowser.open usa il BROWSER PREDEFINITO su ogni sistema
                webbrowser.open(url)  # noqa: S606
                return f"Apro {alias} nel browser."
        m = re.search(r"(?:vai (?:su|a)|portami su|apri)\s+([a-z0-9\.\-]+\.[a-z]{2,})", t)
        if m:
            url = "https://" + m.group(1)
            webbrowser.open(url)  # noqa: S606
            return f"Apro {m.group(1)}."
        return "Non ho capito quale sito aprire."

    if intent == "time":
        now = datetime.now()
        return f"Sono le {now.strftime('%H e %M')}."

    if intent == "date":
        days = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
        months = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
                  "agosto", "settembre", "ottobre", "novembre", "dicembre"]
        now = datetime.now()
        return f"Oggi e' {days[now.weekday()]} {now.day} {months[now.month - 1]} {now.year}."

    if intent == "set_app_volume":
        t2 = _strip_wake(t)  # 'chicco volume di comet al 60' -> 'volume di comet al 60'
        app = _appvol_app_name(t2)
        if not app:
            sess = sorted({p for p, _, _ in _audio_sessions()})
            return ("Di quale applicazione vuoi che regoli il volume? "
                    f"App con audio attivo in questo momento: {', '.join(sess) or 'nessuna'}.")
        pv = _parse_app_volume(t2)
        if pv is None:
            if any(w in t2 for w in ("abbassa", "alza", "aumenta", "diminuisci",
                                     "riduci", "imposta", "metti", "porta",
                                     "regola", "tira", "azzer", "silenz")):
                mode = "down" if any(w in t2 for w in ("abbassa", "diminuisci",
                                                       "riduci", "azzer")) else "up"
                return _set_app_volume(app, mode, 10)  # relativo senza numero
            return _get_app_volume(app)  # 'volume di discord?': solo lettura
        mode, level = pv
        return _set_app_volume(app, mode, level)

    if intent == "volume":
        # muto: toggle istantaneo (prima di qualsiasi altra interpretazione)
        if "muto" in t or "mute" in t:
            try:
                vol = _endpoint_volume()
                new_mute = not bool(vol.GetMute())
                vol.SetMute(int(new_mute), None)
                return "Audio escluso." if new_mute else "Audio riattivato."
            except Exception as exc:
                print(f"[volume] errore pycaw (muto): {exc}; uso fallback tasti")
            if pu.IS_WINDOWS:
                _popen(["powershell", "-NoProfile", "-Command",
                                  "$w=New-Object -ComObject WScript.Shell; $w.SendKeys('{VK_VOLUME_MUTE}')"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            elif pu.IS_LINUX:
                _popen(["sh", "-c", pu.pactl_or_alsa("mute")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "Comando muto inviato."
        level, mode = _parse_volume(t)
        try:
            vol = _endpoint_volume()
            cur = int(round(vol.GetMasterVolumeLevelScalar() * 100))
            if level is None:               # 'alza/abbassa il volume' -> +-10
                level = 10
                mode = "up" if ("alza" in t or "aumenta" in t) else "down"
            if mode == "abs":
                new = level
            elif mode == "up":
                new = min(100, cur + level)
            else:
                new = max(0, cur - level)
            vol.SetMasterVolumeLevelScalar(new / 100.0, None)
            got = int(round(vol.GetMasterVolumeLevelScalar() * 100))  # verifica reale
            if abs(got - new) > 2:
                return f"Non sono riuscito a portare il volume al {new} per cento."
            return f"Volume portato al {got} per cento."
        except Exception as exc:
            print(f"[volume] errore pycaw: {exc}; uso fallback tasti")
        # fallback tasti multimediali (Windows) o pactl/amixer (Linux)
        try:
            cur = int(round(_endpoint_volume().GetMasterVolumeLevelScalar() * 100))
        except Exception:
            cur = 50
        if mode == "abs" and level is not None:
            mode = "up" if level > cur else "down"
            steps = max(0, min(50, abs(level - cur) // 2))
        else:
            steps = 10
        if pu.IS_WINDOWS:
            key = "{VK_VOLUME_UP}" if mode == "up" else "{VK_VOLUME_DOWN}"
            if steps:
                ps = (f"$w=New-Object -ComObject WScript.Shell; "
                      f"1..{steps} | ForEach-Object {{ $w.SendKeys('{key}') }}")
                _popen(["powershell", "-NoProfile", "-Command", ps],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        elif pu.IS_LINUX:
            pu.press_media_keys(steps if mode == "up" else 0,
                                steps if mode == "down" else 0)
        return "Volume regolato approssimativamente."

    if intent == "list_files":
        loc = extract_location(text)
        try:
            items = sorted(p.name for p in loc.iterdir())
        except Exception:
            return f"Non riesco a leggere la cartella {loc}."
        preview = ", ".join(items[:12])
        more = f" e altri {len(items) - 12}" if len(items) > 12 else ""
        return f"In {loc.name} trovo: {preview}{more}."

    # niente comando riconosciuto: fallback AGENTICO — il piccolo modello
    # locale risponde alla domanda ("quanto fa 1+1", "chi ha inventato il
    # telefono") invece del secco "non ho capito". Se Ollama non risponde
    # resta il messaggio preimpostato con la lista delle funzioni.
    chat = ollama_chat(text)
    if chat:
        return chat
    return ("Non ho capito il comando. Posso creare o eliminare cartelle, aprire app e "
            "siti, darti ora e data, regolare il volume o elencare i file.")


# ---------------------------------------------------------------------------
# Pipeline completa
# ---------------------------------------------------------------------------
# Conferma vocale delle correzioni 'molto diverse': se Qwen riscrive la
# trascrizione radicalmente, Ugo chiede 'Hai detto ...?' ed esegue solo
# dopo un si' vocale (o annulla con no). Scopo dopo PENDING_TTL secondi.
CONFIRM_RATIO = 0.55
PENDING_TTL = 90.0
_pending = {"text": None, "app": None, "ts": 0.0}
_pending_choices = {"menu": None}   # lista nomi app per la scelta vocale numerata
_ORDINALS = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5,
             "primo": 0, "prima": 0, "secondo": 1, "seconda": 1,
             "terzo": 2, "terza": 2, "quarto": 3, "quarta": 3,
             "quinto": 4, "quinta": 4, "sesto": 5, "sesta": 5,
             "ultimo": -1, "ultima": -1}


def _pick_ordinal(text: str) -> int | None:
    """Indice 0-based se la frase E' solo una scelta numerata ('primo',
    'la seconda', 'numero 3', 'sugo' no): None altrimenti."""
    tokens = re.findall(r"[a-z0-9à-ù]+", (text or "").lower().strip())
    if not tokens or len(tokens) > 2:
        return None
    fill = {"il", "la", "lo", "l", "numero", "n", "scelgo", "scelta",
            "quello", "quella"}
    core = [t for t in tokens if t not in fill]
    if len(core) != 1 or core[0] not in _ORDINALS:
        return None
    i = _ORDINALS[core[0]]
    if i == -1:  # 'ultimo': si risolve al momento dell'uso
        return -1
    return i
_YES = {"si", "sì", "ok", "okay", "confermo", "conferma", "esatto", "esatta",
        "giusto", "giusta", "certo", "certamente", "appunto", "sicuro",
        "corretto", "corretta", "esegui", "vai", "yes", "sure", "quoto"}
_YES_REPLIES = {"si", "sì", "ok", "okay", "confermo", "esatto", "giusto",
                "certo", "appunto", "sicuro", "corretto", "corretta",
                "esegui", "vai", "yes", "sure", "quoto"}
_NO = {"no", "nope", "annulla", "annullare", "cancella", "sbagliato",
       "sbagliata", "falso", "falsa", "riprova", "stop", "negativo",
       "non", "niente", "mica"}


def _yes_no(text: str):
    """True (affermazione), False (negazione) o None (non e' una risposta).
    Tollerante ai near-miss dello STT ('confirmo' -> 'confermo').
    Vale SOLO per risposte brevi: frasi piu' lunghe o con un verbo di
    comando ('vai e apri spotify') NON sono conferme, sono comandi."""
    tokens = re.findall(r"[a-zà-ù]+", (text or "").lower())
    if not tokens or len(tokens) > 3:  # una conferma e' breve: 'si', 'ok va bene'
        return None
    tset = set(tokens)
    # il comando contiene un verbo d'azione? non e' una risposta, e' un comando
    # ('vai e apri spotify' prima veniva mangiato come conferma da 'vai')
    if any(re.search(rf"\b{re.escape(w)}\b", (text or "").lower())
           for w in ("apri", "lancia", "avvia", "chiudi", "ferma", "crea",
                     "elimina", "cancella", "cerca", "trova", "scrivi",
                     "imposta", "metti", "dimmi", "che")):
        return None
    if tset & _NO:
        return False
    if tset & _YES_REPLIES:
        return True
    # fuzzy: la voce (soprattutto quella sintetica) viene sentita storta
    for tok in tokens:
        if difflib.get_close_matches(tok, _NO, n=1, cutoff=0.82):
            return False
        if difflib.get_close_matches(tok, _YES, n=1, cutoff=0.82):
            return True
    return None


def qwen_app_suggest(name: str) -> dict | None:
    """Suggerisce quale app installata intendeva l'utente quando ricerca e
    fuzzy non hanno trovato nulla. Ritorna UNA app ('qwen_pick') oppure, se
    c'e' ambiguita', il menu dei candidati per la scelta vocale numerata
    ('qwen_menu'). Difesa: difflib candidati vicini, Qwen sceglie tra nomi
    reali, il pick deve essere uno dei candidati."""
    """Suggerisce quale app installata intendeva l'utente quando ricerca e
    fuzzy non hanno trovato nulla (es. 'stimolo' -> Steam).
    Struttura a difesa: 1) difflib produce i candidati piu' vicini; 2) se il
    migliore e' troppo lontano non si propone nulla (garbage in -> niente);
    3) Qwen fa SOLO scelta multipla tra nomi reali; 4) il pick deve essere
    uno dei candidati. L'utente conferma prima dell'avvio, comunque."""
    try:
        apps = appindex.get_apps()
        norm = appindex._norm
        nq = norm(name)
        if not nq:
            return None
        close = difflib.get_close_matches(nq, [norm(a["name"]) for a in apps],
                                          n=5, cutoff=0.45)
        if not close:
            return None
        # il migliore troppo lontano = il richiesto non somiglia a nessuna app
        if difflib.SequenceMatcher(None, nq, close[0]).ratio() < 0.5:
            return None
        by_norm = {norm(a["name"]): a for a in apps}
        cand_names = [by_norm[c]["name"] for c in close if c in by_norm]
        if not cand_names:
            return None
        import urllib.request
        payload = json.dumps({
            "model": _llm_model(),
            "system": (f'The user asked to open "{name}" but it is not installed. '
                       'Which ONE of these installed apps did they most likely mean? '
                       'Reply with the exact name of one candidate, or NONE if none '
                       'is plausible. Reply with the name only, nothing else. '
                       f'Candidates: {", ".join(cand_names)}'),
            "prompt": "open",
            "stream": False, "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 40},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        _q0 = time.time()
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        _track("qwen_suggest", time.time() - _q0)
        _track_tps("qwen_suggest", data.get("eval_count", 0),
                   data.get("eval_duration", 0))
        out = (data.get("response") or "").strip().strip('"').strip()
        if not out or "\n" in out or len(out) > 60:
            return None
        low = out.lower()
        picked = None
        for app in appindex.search(out, limit=5):
            if app["name"].lower() == low and app["name"] in cand_names:
                picked = app  # valido solo se e' ESATTAMENTE uno dei candidati
                break
        if picked is None:
            return None
        by_name = {a["name"]: a for a in apps}
        alt = [by_name[c] for c in cand_names if c != picked["name"] and c in by_name]
        if len(alt) >= 1 and difflib.SequenceMatcher(
                None, norm(picked["name"]), norm(alt[0]["name"])).ratio() >= 0.62:
            # ambiguo: torna il menu completo (pick + alternative vicine)
            return {"qwen_menu": [picked["name"]] + [a["name"] for a in alt[:2]]}
        return picked
    except Exception as exc:
        print(f"[open_app] qwen_app_suggest errore: {exc}")
        return None


def _translate_reply_it_en(reply: str) -> str:
    """Con lingua EN traduce la risposta italiana in inglese (Qwen, guardie:
    non numeri/tempi puri, non troppo lunga, fallback all'originale)."""
    if piper_tts.current_lang() != "en" or not reply or not re.search(r"[a-zA-Z\u00c0-\u00ff]", reply):
        return reply
    # interrogativi/numeri gia' inglesi o tempo: lascia stare (evita SPOKEN clock
    # tradotto male); le frasi corte generiche invece si traducono
    try:
        import urllib.request
        payload = json.dumps({
            "model": _llm_model(),
            "system": ('Translate this Italian assistant reply into natural '
                       'English. Answer ONLY with the translation. Keep app/site '
                       'names, numbers and units as-is. If it is already English '
                       'or you are unsure, repeat it unchanged.'),
            "prompt": reply,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 120},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        _q0 = time.time()
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        _track("qwen_translate", time.time() - _q0)
        _track_tps("qwen_translate", data.get("eval_count", 0),
                   data.get("eval_duration", 0))
        out = (data.get("response") or "").strip().strip('"').strip()
        if out and "\n" not in out and len(out) <= len(reply) * 3 + 80:
            return out
    except Exception as exc:
        print(f"[lang] traduzione risposta fallita ({exc}); resta l'italiano")
    return reply


def _emit(user: str, reply: str, intent: str, src: str, source: str, dt: int,
          raw: str | None = None, show_list: bool = False) -> dict:
    """Costruisce l'entry di risposta: history, log, voce e ritorno API."""
    spoken = _translate_reply_it_en(reply)   # lingua EN: parla/traduce in inglese
    entry = {
        "user": user, "assistant": spoken, "intent": intent,
        "detector": src, "input": source, "ms": dt, "ts": datetime.now().isoformat(timespec="seconds"),
    }
    if raw:
        entry["raw"] = raw
    if show_list:
        entry["show_list"] = True
    with _log_lock:
        _history.append(entry)
        if len(_history) > 200:
            del _history[:-200]
    print(f"[cmd] intent={intent} via {src} ({dt} ms): {user!r} -> {spoken!r}")
    _reqlog(f"CMD  {source:5s} intent={intent:<12s} via {src:<12s} {dt:5d} ms  "
            f"{user!r} -> {spoken!r}")
    try:
        speak(spoken)
    except Exception as exc:
        print(f"[tts] errore: {exc}")
    return entry


def process(text: str, source: str) -> dict:
    raw_stt, corrected = text, None
    # --- scelta numerata in attesa ('primo', 'la seconda', 'numero 3') ---
    # prima del si/no: 'sì' non è un numero e viceversa, ma l'ordine conta
    # per il menu che sopravvive alla risposta sbagliata
    pick = _pick_ordinal(text or "")
    if pick is not None and _pending_choices["menu"]:
        with _log_lock:
            menu = list(_pending_choices["menu"])
            pend_said = _pending.get("raw_said")
            pend_ts = _pending["ts"]
            _pending["text"] = _pending["app"] = None
            _pending["raw"] = None
            _pending["raw_said"] = None
            _pending_choices["menu"] = None
        if pend_ts and time.time() - pend_ts <= PENDING_TTL:
            if pick == -1:
                pick = len(menu) - 1            # 'ultimo'
            if 0 <= pick < len(menu):
                app_name = menu[pick]
                print(f"[confirm] scelta numerata {pick + 1}: {app_name!r}")
                if pend_said:  # impara: quella parola strana -> l'app scelta
                    _learn_app_alias(pend_said, app_name)
                reply = run_command(f"apri {app_name}", "open_app")
                return _emit(f"apri {app_name}", reply, "open_app", "guard-scelta",
                             source, 0)
            reply = (f"C'e' solo {'una' if len(menu) == 1 else str(len(menu))} "
                     f"scelta: riprova.")
            return _emit(text, reply, "confirm", "guard-scelta", source, 0)
        reply = "Non c'e' piu' nessuna scelta in attesa."
        return _emit(text, reply, "confirm", "guard-scelta", source, 0)
    if pick is not None and not _pending_choices["menu"]:
        # numero detto senza menu aperto: ambiguo ('due' potrebbe essere volume)
        # -> si comporterà come comando normale (prosegue sotto)
        pass
    # --- conferma in attesa ('si' esegue, 'no' annulla) ---
    # NOTA: il controllo va SEMPRE prima della normalizzazione Qwen, che
    # riscriverebbe 'si confermo' in 'Conferma.' mandando in crash la logica
    if _yes_no(text or "") is not None:
        decision = _yes_no(text)
        with _log_lock:
            pend_text, pend_app, pend_ts = _pending["text"], _pending["app"], _pending["ts"]
            pend_raw = _pending.get("raw")
            pend_said = _pending.get("raw_said")  # nome DETTO per il suggerimento app
            _pending["text"] = _pending["app"] = None
            _pending["raw"] = None
            _pending["raw_said"] = None
        if pend_ts and time.time() - pend_ts <= PENDING_TTL:
            if decision is True and not pend_app and not pend_text and _pending_choices["menu"]:
                # 'si' alla domanda col menu: accetta la prima scelta (opzione
                # raccomandata da Qwen); il menu resta il posto del "sì" secco
                menu = list(_pending_choices["menu"])
                _pending_choices["menu"] = None
                app_name = menu[0]
                print(f"[confirm] menu confermato col si: {app_name!r}")
                if pend_said:
                    _learn_app_alias(pend_said, app_name)
                reply = run_command(f"apri {app_name}", "open_app")
                return _emit(f"apri {app_name}", reply, "open_app", "guard-scelta",
                             source, 0)
            if decision is True and (pend_app or pend_text):
                if pend_app:  # 'intendavi X?' confermato: avvio E imparo l'alias
                    if pend_said:
                        _learn_app_alias(pend_said, pend_app)
                    print(f"[confirm] app confermata, avvio: {pend_app!r}")
                    reply = run_command(f"apri {pend_app}", "open_app")
                    return _emit(f"apri {pend_app}", reply, "open_app", "guard", source, 0)
                if pend_raw and pend_raw.strip().lower() != pend_text.strip().lower():
                    _learn_fix(pend_raw, pend_text)  # confermato dall'utente: lo imparo
                text = raw_stt = pend_text  # eseguo cio' che Qwen aveva proposto
                print(f"[confirm] comando confermato, eseguo: {text!r}")
                intent, src, t0 = "confirm", "guard", time.time()
                try:
                    reply = _process_inner(text, detect_intent(text)[0], src)
                except Exception as exc:
                    reply = f"Ho avuto un problema tecnico ({type(exc).__name__})."
                    intent = "error:confirm"
                return _emit(text, reply, intent, src, source,
                             int((time.time() - t0) * 1000))
            if decision is False:
                if pend_app and pend_said:
                    _learn_app_forget(pend_said)  # il suggerimento era sbagliato
                _pending_choices["menu"] = None   # annulla anche un menu aperto
                return _emit(text or "no", "Va bene, annullato.", "confirm", "guard", source, 0)
        # si/no ma conferma assente o scaduta: NON e' mai un comando (prima
        # 'si' veniva classificato 'chiudi sihost' e si tentava il force-kill!)
        return _emit(text, "Non c'e' piu' nulla da confermare.",
                     "confirm", "guard", source, 0)
    else:
        # si/no fuori da qualunque conferma: NON un comando (prima 'si' veniva
        # classificato 'chiudi sihost' e il sistema tentava la chiusura forzata!)
        if _yes_no(text or "") is not None:
            return _emit(text, "Non c'e' nulla da confermare.",
                         "confirm", "guard", source, 0)
        with _log_lock:  # un nuovo comando fa decadere eventuali conferme
            _pending["text"] = _pending["app"] = None
            _pending_choices["menu"] = None

    if text:
        # fase -3: creazione vocale di una routine
        # ("Ugo, quando dico modo gaming esegui apri steam; apri discord; volume 80")
        mnew = re.search(r"quando dico\s+(.{2,60}?)(?:,\s*)?esegui\s+(.+)",
                         _strip_wake(text), re.IGNORECASE)
        if mnew:
            trig = mnew.group(1).strip(" \"'").strip()
            body = mnew.group(2).strip(" .!")
            steps = [s.strip(" .!") for s in
                     re.split(r"\s*(?:;|\be\s+poi\b|\bpoi\b)\s*", body) if s.strip()][:ROUTINE_MAX_STEPS]
            if trig and steps:
                rid = re.sub(r"[^a-z0-9]+", "_", _norm(trig))[:40] or f"r_{int(time.time())}"
                with _learned_lock:
                    data = _learned_load()          # UNA sola lettura: la scrittura
                    rs = data.setdefault("__routines__", {})   # deve serializzare QUESTO dict
                    old = rs.get(rid, {})
                    rs[rid] = {"id": rid, "name": trig, "trigger": trig,
                               "steps": steps, "created": old.get("created", time.time()),
                               "count": old.get("count", 0)}
                    try:
                        LEARNED_FILE.write_text(json.dumps(
                            data, ensure_ascii=False, indent=1), encoding="utf-8")
                    except Exception:
                        pass
                reply = (f"Routine '{trig}' {'aggiornata' if old else 'creata'}: "
                         f"{len(steps)} passi. La attivi dicendo '{trig}'.")
                return _emit(text, reply, "routine_created", "macro", source, 0)
        # fase -2: esecuzione routine (macro vocali): 'modo gaming', 'serata film'...
        try:
            rout = _find_routine(text)
        except Exception:
            rout = None
        if rout:
            replies = _routine_execute(rout)
            _routine_bump(rout.get("id", ""))
            name = rout.get("name") or "routine"
            summary = " ".join(replies)[:220]
            reply = f"Eseguo {name}. {summary}" if replies else f"{name}: nessun passo eseguibile."
            return _emit(text, reply, "routine", "macro", source, 0)
        # fase -1.5: comandi multipli ("apri youtube e discord", "muto e apri
        # spotify") -> eseguiti IN SEQUENZA con un riassunto parlato unico
        # (stessa filosofia delle routine: un solo bubbles/tts, le risposte
        # si taglierebbero a vicenda). Lo splitter e' conservativo: in dubbio
        # non spezza e la frase prosegue nella pipeline normale.
        try:
            subcmds = multicommand.split_commands(_strip_wake(text))
        except Exception:
            subcmds = None
        if subcmds and len(subcmds) > 1:
            t0 = time.time()
            replies = []
            for sc in subcmds[:multicommand.MAX_SUBCOMMANDS]:
                try:
                    replies.append(run_command(sc, detect_intent(sc)[0]))
                except Exception as exc:
                    replies.append(f"{sc}: errore ({type(exc).__name__})")
            reply = " e ".join(r for r in replies if r)[:400]
            return _emit(text, reply, "multi", "multi", source,
                         int((time.time() - t0) * 1000))
        # fase -1: alias-app gia' confermati in passato -> apre SUBITO, senza
        # neppure chiedere a Qwen ('stimolo' -> Steam in ~40 ms, zero LLM)
        m = re.match(r"^(apri|lancia|avvia|chiudi)\s+(.{2,40})$",
                     _strip_wake((text or "").lower().strip()))
        if m:
            target = re.sub(r"^(il|lo|la|l'|un|una|mi)\s+",
                            "", m.group(2).strip(" .!"))
            target = _strip_wake(target)  # 'chicco apri steam' -> 'apri steam'
            # AMBIGUITA' pre-Qwen: piu' di un'app molto simile al target detto
            # (Steam/SteamVR, Code/Code-Insiders) -> menu a scelta numerata;
            # si puo' rispondere 'primo', 'la seconda'... (o sì = la prima)
            verb = m.group(1)
            if target and not _learned_app_lookup(target):
                close_all = difflib.get_close_matches(
                    appindex._norm(target),
                    [appindex._norm(a["name"]) for a in appindex.get_apps()],
                    n=3, cutoff=0.62)
                if len(close_all) >= 2:
                    # l'app esatta esiste gia' (match quasi perfetto)? niente menu
                    best_ratio = difflib.SequenceMatcher(
                        None, appindex._norm(target), close_all[0]).ratio()
                    if best_ratio < 0.9:
                        by_n = {appindex._norm(a["name"]): a
                                for a in appindex.get_apps()}
                        menu = [by_n[c]["name"] for c in close_all if c in by_n][:3]
                        with _log_lock:
                            _pending["app"] = None
                            _pending["raw_said"] = target
                            _pending["text"] = None
                            _pending["ts"] = time.time()
                            _pending_choices["menu"] = menu
                        opts = " o ".join(f"{i + 1}) {n}" for i, n in enumerate(menu))
                        return _emit(text, f'Quale intendevi: {opts}? '
                                           'Dimmi primo, secondo o terzo.',
                                     "open_app", "scelta", source, 0)
            # i refusi appresi si applicano gia' qui, pre-intent: il comando
            # girato viene riscritto e tutto il resto lo vede corretto
            fixed_target = _learned_token_fix(target)
            if fixed_target != target:  # confronto ESATTO: conta anche il case
                # ('spotify' -> 'Spotify' significa che la memoria ha matchato)
                # il nome riscritto risolve gia' nell'indice? apri subito:
                # niente Qwen, niente pipeline (risoluzione pre-intent)
                hits0 = appindex.search(fixed_target, limit=1)
                if hits0:
                    intent = ("open_app" if m.group(1) in ("apri", "lancia", "avvia")
                              else "close_app")
                    reply = run_command(f"{m.group(1)} {hits0[0]['name']}", intent)
                    return _emit(text, reply, intent, "learned", source, 0)
                text = f"{m.group(1)} {fixed_target}"  # il resto lo vede corretto
                target = fixed_target
            alias_app = _learned_app_lookup(target)
            if alias_app:
                intent = ("open_app" if m.group(1) in ("apri", "lancia", "avvia")
                          else "close_app")
                reply = run_command(f"{m.group(1)} {alias_app}", intent)
                return _emit(text, reply, intent, "alias", source, 0)
        # fase 0: Qwen corregge errori di dettato/trascrizione prima di tutto
        # (voce E testo: anche chi scrive sbaglia a digitare 'apri spotrifyt')
        cand = safe_normalize(text)
        if cand:
            if (source == "voce" and difflib.SequenceMatcher(
                    None, raw_stt.lower(), cand.lower()).ratio() < CONFIRM_RATIO):
                # correzione radicalmente diversa: niente esecuzione, chiedo
                with _log_lock:
                    _pending["text"] = cand
                    _pending["app"] = None
                    _pending["raw"] = raw_stt
                    _pending["ts"] = time.time()
                print(f"[confirm] correzione troppo diversa, chiedo: {raw_stt!r} -> {cand!r}")
                return _emit(raw_stt, f'Hai detto: "{cand}"? Rispondi sì o no.',
                             "confirm", "guard", source, 0)
            corrected, text = cand, cand
            print(f"[normalize] {raw_stt!r} -> {cand!r}")
    intent, src = detect_intent(text)
    t0 = time.time()
    try:
        reply = _process_inner(text, intent, src)
    except Exception as exc:
        # rete di sicurezza: nessun errore deve uccidere la richiesta
        print(f"[cmd] ERRORE su {text!r}: {type(exc).__name__}: {exc}")
        reply = ("Ho avuto un problema tecnico nell'eseguire il comando "
                 f"({type(exc).__name__}). Riprova o riformula.")
        intent, src = f"error:{intent}", src
    dt = int((time.time() - t0) * 1000)
    with _log_lock:
        items = list(_last_items) if intent == "list_apps" else []
    return _emit(text, reply, intent, src, source, dt,
                 raw=raw_stt if corrected else None, show_list=bool(items))


def _process_inner(text: str, intent: str, src: str) -> str:
    if intent == "unknown":
        # nessuna regola e Laya non sicuri: prova il piccolo modello LLM
        spec = ollama_parse(text)
        if spec and spec.get("action") and spec["action"] != "unknown":
            return execute_spec(spec, text)
    return run_command(text, intent)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(PKG_DIR / "ui.html")


@app.post("/api/listen")
async def api_listen(audio: UploadFile):
    """Riceve l'audio dal browser (webm/opus), lo trascrive con Vosk ed esegue."""
    data = await audio.read()
    # decodifica webm -> pcm 16k mono tramite ffmpeg (se presente)
    try:
        # NOTA: niente _run qui (metterebbe capture_output=True, incompatibile
        # con stdout/stderr espliciti); input binario, quindi neanche text=True.
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le",
             "-ac", "1", "-ar", "16000", "pipe:1"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
            **_WFLAGS,
        )
    except FileNotFoundError:
        return JSONResponse({"error": "ffmpeg non trovato: installalo per lo STT dal browser"},
                            status_code=500)
    return _handle_pcm(proc.stdout)


@app.post("/api/listen_wav")
async def api_listen_wav(request: Request, wake: int = 0):
    """Riceve un WAV PCM (widget desktop), lo trascrive ed esegue.
    Con wake=1 (ascolto passivo) PRIMA verifica con Whisper che nella frase
    ci sia davvero la wake word: i falsipositivi del rilevatore economico
    (Vosk/OWW sul rumore, TV, conversazioni) non eseguono piu' comandi."""
    data = await request.body()
    _t0 = time.time()
    try:
        res = _handle_pcm(_wav_to_pcm16k(data), require_wake=bool(wake))
        _reqlog(f"HTTP  /api/listen_wav wake={wake}  {len(data)} B  "
                f"{time.time() - _t0:.2f}s  -> {(res.get('assistant') or res.get('error') or '?')!r}")
        return res
    except Exception as exc:
        _reqlog(f"HTTP  /api/listen_wav wake={wake}  ERRORE: {exc}")
        return JSONResponse({"error": f"WAV non valido: {exc}"}, status_code=400)


def _wav_to_pcm16k(data: bytes) -> bytes:
    """Converte WAV PCM 16 bit (mono/stereo, frequenza qualsiasi) in s16le 16 kHz mono."""
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("atteso PCM 16 bit")
    a = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000:
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n),
                      np.arange(len(a), dtype=np.float64),
                      a.astype(np.float64)).astype(np.int16)
    return a.tobytes()


def _fast_resolve(verb: str, cand: str) -> dict | None:
    """Risolve 'verb + nome app' sull'indice; None se non e' inequivocabile."""
    close = difflib.get_close_matches(
        _norm(cand), [_norm(a["name"]) for a in appindex.get_apps()],
        n=2, cutoff=0.88)
    if len(close) != 1:  # nessun match, o due candidati troppo simili: corsia normale
        return None
    apps = appindex.search(close[0], limit=1)
    if not apps:
        return None
    name = apps[0]["name"]
    intent = "open_app" if verb in ("apri", "lancia", "avvia") else "close_app"
    reply = run_command(f"{verb} {name}", intent)  # verbo italiano: run_command lo capisce
    return _emit(f"{verb} {cand}", reply, intent, "fastlane", "voce", 0)


_FAST_UNSAFE = (
    "sito", "pagina", "google", "youtube", "cartella", "file", "volume",
    "musica", "video", "canzone", "ricerca", "cerca", "trova")
_FAST_TAIL_NOISE = re.compile(
    r"\b(e|ed|poi|quindi|dopodiche|dopo|mentre|per|favore|grazie)\b")

# residui di wake word: "chicco apri spotify" Vosk lo scrive così, e la fase
# pre-intent lo vedrebbe come app "chicco apri spotify" -> nessuna app
_WAKE_RESIDUE = re.compile(
    r"^\s*(?:(?:ehi|oh|hey|e|a|he)\s+)?"
    r"(?:ugo|hugo|sugo|wugo|yugo|jugo|ugoo|uugo|uhgo|riugo|truogo|fuoco)\b[,\s]*",
    re.IGNORECASE)


def _strip_wake(text: str) -> str:
    """Toglie i residui di wake word all'inizio: 'chicco apri spotify' ->
    'apri spotify'. Ripete finche' pulisce ('ehi chicco chicco apri steam')."""
    prev = None
    while prev != (text := _WAKE_RESIDUE.sub("", text, count=1)):
        prev = text
    return text.strip()


def _fast_command(text: str) -> dict | None:
    """Corsia veloce per i comandi vocali piu' comuni.

    Vosk trascrive lo stesso audio in ~0,3 s (Whisper ne impiega ~4 su CPU):
    per 'apri X' / 'chiudi X' con un'app INEQUIVOCABILE nel nome, esegue
    subito senza aspettare Whisper. Tollerante alle parole fantasma di coda
    di Vosk ('chiudi spotify fai'): prova a scartarne fino a due, ma se nel
    comando c'e' una congiunzione ('e', 'poi'...) e' un comando composto e
    decide la pipeline completa. Ritorna None quando non e' abbastanza sicuro.
    """
    t = (text or "").lower().strip()
    t = _strip_wake(t)  # 'chicco apri spotify' -> 'apri spotify' (residuo di wake)
    # volume per-app: 'abbassa il volume di discord al 30' -> subito, senza Whisper
    m = re.match(r"^(abbassa|alza|aumenta|diminuisci|riduci|imposta|metti|tira|"
                 r"azzer\w*|silenz\w*)\b.{0,30}?(?:volume|audio|suono)\s*"
                 r"(?:di|del|della|per)\s+(.+?)\s*$", t)
    if m:
        tail = m.group(2)
        app = _appvol_app_name(t)
        # nessuna congiunzione ('e', 'poi'...) e app con sessione audio attiva:
        # altrimenti decide la pipeline completa
        if (app and not _FAST_TAIL_NOISE.search(tail) and " e " not in f" {tail} "
                and _resolve_audio_process(app)):
            pv = _parse_app_volume(t)
            if pv:
                mode, level = pv
            else:
                level = 10
                mode = ("down" if m.group(1).startswith(
                    ("abbassa", "diminuisci", "riduci", "azzer", "silenz")) else "up")
            return _emit(t, _set_app_volume(app, mode, level),
                         "set_app_volume", "fastlane", "voce", 0)
    m = re.match(r"^(apri|lancia|avvia|chiudi|chiudimi|ferma)\s+(.{2,60})$", t)
    if not m:
        return None
    verb, target = m.group(1), m.group(2).strip(" .!")
    if not target or any(w in target for w in _FAST_UNSAFE):
        return None
    if _FAST_TAIL_NOISE.search(target):  # comando composto: niente scorciatoie
        return None
    # residui di wake word all'inizio ('chicco apri spotify' -> 'apri spotify')
    target = _strip_wake(target)
    # la memoria dei refusi confermati risolve i nomi PRIMA della pipeline:
    # 'apri lo spotifi' -> 'apri Spotify' in ~ms, senza Whisper ne' Qwen
    target = _learned_token_fix(target)
    words = target.split()
    for drop in range(3):  # parole fantasma di coda ('fai', 'la', 'e' gia' escluso)
        cand = " ".join(words[:len(words) - drop]) if drop else target
        cand = re.sub(r"^(il|lo|la|l'|un|una|mi)\s+", "", cand).strip()
        if not cand:
            break
        res = _fast_resolve(verb, cand)
        if res is not None:
            return res
    return None


_WAKE_FUZZY = ("ugo", "hugo", "sugo", "wugo", "yugo", "jugo")


def _text_has_wake(text: str) -> bool:
    """La trascrizione contiene la wake word? (fuzzy: Whisper la storcia
    in 'uga', 'oga', 'u go'...). Controlla i primi token dopo gli eventuali
    riempitivi, con distanza di edit limitata e lunghezza minima."""
    toks = _norm(text).split()
    fillers = {"ehi", "hey", "oh", "ehila", "ciao", "su", "allora", "a"}
    toks = [t for t in toks if t not in fillers][:3]
    for t in toks[:2]:
        if len(t) < 2:
            continue                                   # 'u' da solo: non basta
        if t in _WAKE_FUZZY:
            return True
        for w in _WAKE_FUZZY:
            d = _edit_distance(t, w)
            if d <= 1 or (d <= 2 and len(t) >= 3):
                return True
    return False


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein classica (stringhe cortissime, costo trascurabile)."""
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def _handle_pcm(pcm: bytes, require_wake: bool = False):
    if not pcm:
        return JSONResponse({"error": "audio vuoto"}, status_code=400)
    # Normalizzazione server-side: il browser comprime in Opus e molti micro
    # registrano a ~-40 dBFS; il VAD di faster-whisper scarta la voce debole
    # come silenzio ("Non ho sentito nulla") anche quando Vosk la legge bene.
    # Solo i segnali DEBOLI vengono portati verso RMS 0.2 (max x20).
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if a.size:
        rms = float(np.sqrt(np.mean(a ** 2)))
        if 1e-5 < rms < 0.05:
            g = min(0.2 / rms, 20.0)
            pcm = (np.clip(a * g, -1, 1) * 32767).astype("<i2").tobytes()
            print(f"[livello] audio debole (rms {rms:.4f}): gain {g:.1f}x")
    # corsia veloce: Vosk e' gia' pronto, per i comandi banali non aspetta Whisper
    # (con verifica attiva la corsia vale solo se la wake e' visibile nel testo)
    _tcmd = time.time()
    try:
        vtxt = _vosk_transcribe(pcm)
        _f0 = time.time()
        fast = _fast_command(vtxt) if (not require_wake or _text_has_wake(vtxt)) else None
        _track("fastlane", time.time() - _f0)
        if fast is not None:
            print("[fastlane] comando semplice eseguito senza Whisper")
            _track("command", time.time() - _tcmd)
            return fast
    except Exception as exc:
        print(f"[fastlane] scartata ({exc}); passo alla pipeline completa")
        vtxt = ""
    text = transcribe(pcm)
    if require_wake and not _text_has_wake(text or ""):
        # se c'e' una domanda in attesa (menu scelta o conferma sì/no) la
        # risposta breve ('primo', 'si') non deve contenere la wake word
        awaiting = bool(_pending_choices["menu"] or _pending.get("app")
                        or _pending.get("text"))
        if not awaiting:
            entry = {"user": text, "assistant": "Non ho sentito 'Ugo': riprova "
                     "dicendo prima la wake word.", "intent": "-", "detector": "wake-guard",
                     "input": "voce", "ms": 0, "silent": True,
                     "ts": datetime.now().isoformat(timespec="seconds")}
            with _log_lock:
                _history.append(entry)
            print("[wake-guard] falso positivo scartato:", repr(text))
            _track("command", time.time() - _tcmd)
            return entry
    if not text:
        entry = {"user": "", "assistant": "Non ho sentito nulla, riprova.",
                 "intent": "-", "detector": "-", "input": "voce", "ms": 0,
                 "ts": datetime.now().isoformat(timespec="seconds")}
        with _log_lock:
            _history.append(entry)
        speak(entry["assistant"])
        _track("command", time.time() - _tcmd)
        return entry
    res = process(text, "voce")
    _track("command", time.time() - _tcmd)
    return res


@app.post("/api/text")
async def api_text(payload: dict):
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "testo vuoto"}, status_code=400)
    _t0 = time.time()
    res = process(text, "testo")
    _track("command", time.time() - _t0)  # anche il testo entra nella dashboard
    _reqlog(f"HTTP  /api/text  {time.time() - _t0:.2f}s  "
            f"{text!r} -> {(res.get('assistant') or res.get('error') or '?')!r}")
    return res


@app.post("/api/normalize")
def api_normalize(payload: dict):
    """Corregge una trascrizione STT con Qwen (guardie incluse), SENZA eseguire
    nulla: per mostrare nel widget 'cosa ho sentito vs cosa ho capito'."""
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "testo vuoto"}, status_code=400)
    corrected = safe_normalize(text)
    return {"raw": text, "text": corrected or text, "corrected": bool(corrected)}


@app.get("/api/model")
def api_model_get():
    """Modello LLM attivo + quelli disponibili (menu widget / UI web)."""
    return {"active": _llm_model(), "available": ollama_models()}


# ---------------------------------------------------------------------------
# Lingua globale (bandiera IT/EN della UI web): guida voce Piper, voce di
# sistema, lingua Whisper e la traduzione EN->IT dei comandi. Persistita nel
# prefs di Piper; widget e web la leggono per tradursi in tempo reale.
# ---------------------------------------------------------------------------
@app.get("/api/lang")
def api_lang_get():
    lang = piper_tts.current_lang()
    return {"lang": lang,
            "voice": piper_tts.voice_key_for(lang),
            "voice_ready": piper_tts.voice_ready(lang),
            "downloading": piper_tts.status()["downloading"],
            "pct": piper_tts.status()["pct"]}


@app.post("/api/lang")
def api_lang_set(payload: dict):
    lang = (payload.get("lang") or "").lower()[:2]
    if lang not in ("it", "en"):
        return JSONResponse({"error": "lingua non supportata (it, en)"},
                            status_code=400)
    prev = piper_tts.current_lang()
    piper_tts.set_language(lang)   # seleziona la voce di default accoppiata
    if lang == "en" and not piper_tts.voice_ready("en"):
        piper_tts.ensure_voice("en")  # scarico Amy in background se manca
    if lang != prev:
        print(f"[lang] lingua attiva: {prev} -> {lang} "
              f"(voce: {piper_tts.voice_key_for(lang)})")
    return {"ok": True, "lang": lang,
            "voice": piper_tts.voice_key_for(lang),
            "voice_ready": piper_tts.voice_ready(lang),
            "downloading": piper_tts.status()["downloading"],
            "pct": piper_tts.status()["pct"]}


@app.post("/api/model")
def api_model_set(payload: dict):
    """Cambia il modello LLM (correzione STT, JSON, suggerimenti app)."""
    name = (payload.get("model") or "").strip()
    if not name:
        return JSONResponse({"error": "modello mancante"}, status_code=400)
    if name not in ollama_models():
        return JSONResponse({"error": f"modello non installato in Ollama: {name}"},
                            status_code=400)
    _active_model["name"] = name
    try:
        MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODEL_FILE.write_text(json.dumps({"model": name}))
    except Exception as exc:
        print(f"[model] persistenza fallita: {exc}")
    print(f"[model] attivo: {name}")
    return {"ok": True, "active": name}


@app.get("/api/apps")
def api_apps(q: str | None = Query(default=None)):
    """Libreria completa: app (lnk, Store/AppX, portabili, shell) + giochi.

    Senza 'q' restituisce anche 'categories' per la lista categorizzata della
    UI; con 'q' una lista piatta filtrata per nome.
    """
    apps = appindex.search(q) if q else appindex.get_apps()
    game_items = [{"name": g["name"], "kind": g["launcher"], "target": ""}
                  for g in games.get_games()]
    if q:
        nq = q.lower()
        g = [g for g in game_items if nq in g["name"].lower()]
        return {"count": len(g) + len(apps), "apps": g + apps}
    n_apps = sum(1 for a in apps if a["kind"] in ("lnk", "appx", "portable"))
    n_shell = sum(1 for a in apps if a["kind"] == "shell")
    return {"count": len(game_items) + len(apps), "apps": game_items + apps,
            "games_count": len(game_items),
            "categories": [
                {"name": "Giochi", "count": len(game_items)},
                {"name": "Applicazioni", "count": n_apps},
                {"name": "Strumenti di sistema", "count": n_shell},
            ]}


@app.post("/api/apps/rescan")
def api_apps_rescan():
    n = len(appindex.get_apps(force=True))
    return {"ok": True, "count": n}


@app.get("/api/list")
def api_list():
    """Ultima lista giochi/app richiesta a voce ('quali giochi ho su steam')."""
    with _log_lock:
        items = list(_last_items)
    return {"count": len(items), "items": items}


@app.get("/api/memory")
def api_memory_get():
    """Memoria appresa (per il pannello web): refusi corretti + alias-app."""
    data = _learned_load()
    fixes = sorted((v for k, v in data.items()
                    if k != "__apps__" and "raw" in v),
                   key=lambda v: -v.get("count", 1))
    apps = sorted(data.get("__apps__", {}).values(),
                  key=lambda v: -v.get("count", 1))
    return {"fixes": fixes, "apps": apps}


@app.post("/api/memory")
def api_memory_edit(payload: dict):
    """Modifica manuale della memoria: set_fix/set_app/del_fix/del_app/add_fix.
    Il file su disco resta la fonte di verita': qui si riscrive in sicurezza
    (con lock, gestione errori e normalizzazione minima dell'input)."""
    action = payload.get("action", "")
    raw = (payload.get("raw") or "").strip()
    fixed = (payload.get("fixed") or payload.get("app") or "").strip()
    with _learned_lock:
        data = _learned_load()
        if action == "del_fix":
            for k in list(data.keys()):
                if k != "__apps__" and data[k].get("raw", "").lower() == raw.lower():
                    data.pop(k)
        elif action == "del_app":
            data.get("__apps__", {}).pop(raw.lower(), None)
        elif action in ("set_fix", "add_fix") and raw and fixed:
            if action == "add_fix":
                old = next((v for k, v in data.items()
                            if k != "__apps__" and v.get("raw", "").lower() == raw.lower()), None)
                if old:
                    old["fixed"] = fixed
                else:
                    data[raw.lower()] = {"raw": raw, "fixed": fixed,
                                         "count": 1, "ts": time.time()}
            else:
                for k in list(data.keys()):
                    if k != "__apps__" and data[k].get("raw", "").lower() == raw.lower():
                        data[k]["fixed"] = fixed
                        break
        elif action == "set_app" and raw and fixed:
            entry = data.setdefault("__apps__", {})
            if raw.lower() in entry:
                entry[raw.lower()]["app"] = fixed
            else:
                entry[raw.lower()] = {"said": raw, "app": fixed,
                                      "count": 1, "ts": time.time()}
        else:
            return JSONResponse({"error": "azione o parametri non validi"},
                                status_code=400)
        try:
            LEARNED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    return {"ok": True}


@app.get("/api/routines")
def api_routines_get():
    """Elenco routine (macro vocali) per la pagina web."""
    rs = _learned_load().get("__routines__", {})
    items = sorted(rs.values(), key=lambda r: r.get("created", 0))
    return {"routines": items}


@app.post("/api/routines")
def api_routines_edit(payload: dict):
    """CRUD routine da web: create/update/delete/run. Stesso file di memoria:
    una routine creata da web vale anche a voce, e viceversa."""
    action = payload.get("action", "")
    with _learned_lock:
        data = _learned_load()
        rs = data.setdefault("__routines__", {})
        if action in ("create", "update"):
            rid = (payload.get("id") or "").strip()
            name = (payload.get("name") or "").strip()
            steps = [re.sub(r"\s+", " ", s).strip(" .!")
                     for s in (payload.get("steps") or [])
                     if str(s).strip()][:ROUTINE_MAX_STEPS]
            steps = [s for s in steps if s]
            if not name or not steps:
                return JSONResponse({"error": "nome o passi mancanti"}, status_code=400)
            if not rid:
                rid = re.sub(r"[^a-z0-9]+", "_", _norm(name))[:40] or f"r_{int(time.time())}"
            if rid in rs and action == "create":
                return JSONResponse({"error": "esiste gia' una routine con questo nome"}, status_code=400)
            old = rs.get(rid, {})
            rs[rid] = {"id": rid, "name": name, "trigger": name,
                       "steps": steps, "created": old.get("created", time.time()),
                       "count": old.get("count", 0)}
        elif action == "delete" and payload.get("id"):
            rs.pop(payload["id"], None)
        elif action == "run" and payload.get("id"):
            r = rs.get(payload["id"])
            if not r:
                return JSONResponse({"error": "routine inesistente"}, status_code=404)
            replies = _routine_execute(r)
            r["count"] = r.get("count", 0) + 1
            reply = f"Eseguo {r.get('name')}. " + " ".join(replies)[:200]
            _emit(payload.get("id"), reply, "routine", "macro-web", "web", 0)
            try:
                LEARNED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
            except Exception:
                pass
            return {"ok": True, "reply": reply}
        else:
            return JSONResponse({"error": "azione non valida"}, status_code=400)
        try:
            LEARNED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    return {"ok": True}


@app.get("/api/stt")
def api_stt():
    """Quale trascrittore e' attivo (Whisper modello scelto, o fallback Vosk)."""
    if whisper_available():
        try:
            import ctranslate2 as ct
            gpu = ct.get_cuda_device_count() > 0
        except Exception:
            gpu = False
        return {"engine": "whisper", "model": _whisper_choice["name"],
                "device": "cuda/gpu" if gpu else "cpu"}
    return {"engine": "vosk", "model": "small-it-0.22", "device": "cpu"}


@app.get("/api/stt/models")
def api_stt_models():
    """Catalogo modelli Whisper per la dropdown web (installato/attivo/download)."""
    out = []
    for name, info in WHISPER_CATALOG.items():
        out.append({
            "id": name,
            "label": info["label"],
            "installed": _fw_installed(name),
            "active": name == _whisper_choice["name"] and whisper_available(),
            "downloading": name in _whisper_dl["busy"],
        })
    return {"models": out, "fallback": "vosk"}


@app.post("/api/stt/model")
def api_stt_model_set(payload: dict):
    """Seleziona il modello Whisper. Se non installato: avvia il download in
    background e si torna a Whisper quando e' pronto (nel frattempo Vosk)."""
    name = (payload.get("model") or "").strip()
    if name not in WHISPER_CATALOG:
        return JSONResponse({"error": f"modello sconosciuto: {name}"}, status_code=400)
    _whisper_choice["name"] = name
    try:
        STT_FILE.write_text(json.dumps({"whisper": name}))
    except Exception:
        pass
    if _fw_installed(name):
        with _whisper_lock:  # scarica il modello vecchio dalla RAM
            _whisper["model"] = None
        return {"ok": True, "status": "attivo"}
    if name not in _whisper_dl["busy"]:
        _whisper_dl["busy"].add(name)
        threading.Thread(target=_fw_download, args=(name,), daemon=True).start()
    return {"ok": True, "status": "downloading"}


@app.get("/_tts_reply.wav")
def tts_wav():
    """Ultima risposta vocale generata, riprodotta dalla UI."""
    return FileResponse(BASE / "_tts_reply.wav", media_type="audio/wav")


@app.get("/api/tts")
def api_tts():
    """Stato del motore vocale per la dropdown della UI (Piper naturale / sistema).
    L'etichetta della voce naturale segue la lingua attiva: Paola (it) / Amy (en)."""
    st = piper_tts.status()
    active = piper_tts.get_engine()
    nat = "Amy" if st.get("lang") == "en" else "Paola"
    return {"engines": [
        {"id": "piper", "label": f"Piper — {nat} (naturale, locale)",
         "voice": nat, "installed": st["piper_ready"],
         "downloading": st["downloading"], "active": active == "piper"},
        {"id": "sapi", "label": "Voce di sistema (Elsa)",
         "installed": True, "downloading": False, "active": active == "sapi"},
    ]}


@app.post("/api/tts/engine")
def api_tts_engine(payload: dict):
    """Cambia il motore vocale. Se si sceglie Piper non ancora scaricato,
    avvia il download in background (intanto parla la voce di sistema)."""
    name = (payload.get("engine") or "").strip()
    if name not in ("piper", "sapi"):
        return JSONResponse({"error": "motore sconosciuto"}, status_code=400)
    piper_tts.set_engine(name)
    status = "attivo"
    if name == "piper" and not piper_tts.is_ready():
        piper_tts.download_async()
        status = "downloading"
    return {"ok": True, "status": status}


@app.get("/api/history")
def api_history():
    with _log_lock:
        return {"history": list(reversed(_history[-50:]))}


def _tool_memory_mb() -> list:
    """RAM stimata dei tool del pipeline. Ollama e' letta dal processo reale;
    laya/vosk/whisper dai modelli caricati IN QUESTO processo (il loro peso e'
    dentro il rss globale: queste stime mostrano come e' distribuito)."""
    tools = []
    try:
        import psutil
        me = psutil.Process()
        # Ollama: processo server + runner del modello (esclusi i figli del nostro)
        ollama_mb = 0.0
        for p in psutil.process_iter(["name", "memory_info"]):
            n = (p.info["name"] or "").lower()
            if "ollama" in n:
                try:
                    ollama_mb += p.info["memory_info"].rss / 1048576
                except Exception:
                    pass
        if ollama_mb:
            tools.append({"name": "Ollama (Qwen)", "mb": round(ollama_mb),
                          "kind": "llm"})
    except Exception:
        pass
    # stime interne: il peso dei modelli caricati nel processo server.
    # Whisper compare SEMPRE che sia installato: se il modello e' ancora
    # lazy (carica al primo comando) lo si dice nella nota, cosi' la board
    # mostra il quadro completo della RAM anche a freddo.
    if laya_system is not None:
        tools.append({"name": "Laya (intent)", "mb": 450, "kind": "intent"})
    if _stt.get("model") is not None:
        tools.append({"name": "Vosk (fallback STT)", "mb": 45, "kind": "stt"})
    if _whisper.get("model") is None and whisper_available():
        wdir = whisper_model_dir().lower()
        est = 2100 if "turbo" in wdir else (600 if "small" in wdir else 200)
        tools.append({"name": f"Whisper {_whisper_choice['name']}",
                      "mb": est, "kind": "stt",
                      "note": "non caricato (al primo comando)"})
    if _whisper.get("model") is not None:
        wdir = whisper_model_dir().lower()
        est = 2100 if "turbo" in wdir else (600 if "small" in wdir else 200)
        tools.append({"name": f"Whisper {_whisper_choice['name']}",
                      "mb": est, "kind": "stt"})
    try:
        import ctranslate2 as _ct
        if _ct.get_cuda_device_count() > 0:
            for t in tools:
                if t["kind"] == "stt" and "Whisper" in t["name"]:
                    t["note"] = "su GPU (VRAM)"
    except Exception:
        pass
    return tools


@app.get("/api/stats")
def api_stats():
    """Dashboard latenze: medie/p95 per fase (ultimi 50 campi ciascuna),
    modelli attivi e stato del processo. Leggero: nessun lavoro pesante."""
    import os as _os
    with _stats_lock:
        snap = {k: list(v) for k, v in _stats.items()}
    out = {}
    for k, v in snap.items():
        s = sorted(v)
        p95 = s[min(len(s) - 1, int(round(0.95 * len(s))) - 1)] if s else 0.0
        out[k] = {"n": len(v), "avg": round(sum(v) / len(v), 3),
                  "p95": round(p95, 3), "max": round(max(v), 3),
                  "last": round(v[-1], 3)}
    # ordinamento di pipeline: prima la voce in ingresso, poi l'interpretazione,
    # poi la voce in uscita e il totale
    order = ["vosk", "fastlane", "whisper", "qwen_normalize", "qwen_intent",
             "qwen_chat", "qwen_suggest", "tts_piper", "tts_sapi", "command"]
    stages = [{"stage": k, **out[k]} for k in order if k in out]
    stages += [{"stage": k, **v} for k, v in out.items() if k not in order]
    with _stats_lock:
        tps = {k: list(v) for k, v in _tps.items()}
    for st in stages:
        v = tps.get(st["stage"])
        if v:
            st["tps_avg"] = round(sum(v) / len(v), 1)
    proc = {}
    try:
        import psutil
        p = psutil.Process()
        mem = p.memory_info().rss
        cpu = p.cpu_percent(interval=None)   # dall'ultimo campione
        proc = {"rss_mb": round(mem / 1048576, 1), "cpu_pct": round(cpu, 1),
                "threads": p.num_threads()}
    except Exception:
        proc = {}
    return {"stages": stages, "process": proc, "tools": _tool_memory_mb(),
            "models": {"stt": _whisper_choice["name"],
                       "llm": _llm_model(),
                       "tts": (f"piper-{piper_tts.voice_key_for(piper_tts.current_lang()).split('-')[1]}"
                               if piper_tts.is_ready() else "sistema")},
            "enabled": _stats_enabled["on"],
            "ts": datetime.now().isoformat(timespec="seconds")}


@app.post("/api/stats")
def api_stats_toggle(payload: dict):
    """Attiva/sospende la raccolta (la UI la mette in pausa quando vuole)."""
    _stats_enabled["on"] = bool(payload.get("enabled", True))
    return {"ok": True, "enabled": _stats_enabled["on"]}


@app.post("/api/reload_stt")
def api_reload_stt():
    _stt["model"] = None
    _stt["recognizer"] = None
    get_stt()
    return {"ok": True, "message": "Riconoscimento ricaricato."}


if __name__ == "__main__":
    import uvicorn

    print(f"Assistente vocale locale su http://127.0.0.1:{PORT}")
    get_stt()  # pre-carica Vosk
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
