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

import games
import io
import appindex
import difflib
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
import wave
import webbrowser
from datetime import datetime
from pathlib import Path

import laya
import pyttsx3
import send2trash
from fastapi import FastAPI, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from vosk import KaldiRecognizer, Model as VoskModel

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
PORT = 8123
PKG_DIR = Path(__file__).resolve().parent
BASE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "chicco"
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
FOLDER_MAP = {
    "desktop": HOME / "Desktop",
    "scrivania": HOME / "Desktop",
    "documenti": HOME / "Documents",
    "documents": HOME / "Documents",
    "download": HOME / "Downloads",
    "scaricati": HOME / "Downloads",
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
# Whisper large-v3-turbo (GGUF Q8_0) via transcribe.cpp + Vulkan (AMD 9070 XT).
# Se il modello non c'e' o WHISPER=0, si ricade su Vosk piccolo.
# ---------------------------------------------------------------------------
WHISPER_GGUF = os.path.expanduser("~/.cache/whisper/whisper-large-v3-turbo-Q8_0.gguf")
_whisper = {"model": None}
_whisper_lock = threading.Lock()

try:
    laya_system = laya.load("convaiinnovations/laya")  # checkpoint inglese
except Exception as exc:  # laya opzionale: senza, si usa solo il matching testuale
    print(f"[laya] non disponibile ({exc}); uso solo regole testuali")
    laya_system = None

print("[apps] scansione libreria applicazioni in background...")
appindex.scan_async()  # lnk + Microsoft Store/AppX + portabili, all'avvio


# ---------------------------------------------------------------------------
# TTS (Elsa, italiano)
# ---------------------------------------------------------------------------
def _pick_voice(engine) -> None:
    for v in engine.getProperty("voices"):
        name = (v.name or "").lower()
        if "ital" in name or "elsa" in name or "it-it" in str(getattr(v, "id", "")).lower():
            engine.setProperty("voice", v.id)
            return


def speak(text: str) -> str:
    """Riproduce la risposta a voce e ritorna il path del file wav generato."""
    out = BASE / "_tts_reply.wav"
    with _tts_lock:
        engine = pyttsx3.init()
        _pick_voice(engine)
        engine.setProperty("rate", 175)
        engine.save_to_file(text, str(out))
        engine.runAndWait()
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
    """Carica il modello Whisper una sola volta; resta sulla GPU via Vulkan."""
    if _whisper["model"] is None:
        with _whisper_lock:
            if _whisper["model"] is None:
                print("[whisper] caricamento large-v3-turbo Q8_0 (Vulkan)...")
                import transcribe_cpp as tc
                _whisper["model"] = tc.Model(WHISPER_GGUF)
    return _whisper["model"]


def whisper_available() -> bool:
    return os.environ.get("WHISPER", "1") == "1" and os.path.isfile(WHISPER_GGUF)


def _whisper_transcribe(pcm16: bytes) -> str:
    """PCM s16le 16k mono -> testo con Whisper turbo (float32, GPU)."""
    a = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    model = get_whisper()
    with model.session() as s:
        return (s.run(a).text or "").strip()


def _vosk_transcribe(pcm: bytes) -> str:
    """Trascrive PCM s16le 16 kHz mono con Vosk; riconoscitore fresco per richiesta."""
    rec = KaldiRecognizer(get_stt(), 16000)
    rec.AcceptWaveform(pcm)
    return json.loads(rec.FinalResult()).get("text", "").strip()


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
OLLAMA_MODEL = "qwen2.5:0.5b"
OLLAMA_SCHEMA = (
    'Convert an Italian voice command into ONE JSON object for a PC assistant. '
    'Allowed actions: create_folder{name,location}, delete_folder{name,location}, '
    'create_file{name,content,location}, append_file{name,content,location}, '
    'delete_file{name,location}, read_file{name,location}, open_app{app}, '
    'open_site{site}, search_web{query,site}, volume{direction}, time{}, date{}, unknown{}. '
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


def normalize_stt(text: str) -> str | None:
    """Ritorna la trascrizione corretta da Qwen, o None se non disponibile.
    La pipeline usa il risultato solo se effettivamente diverso dall'originale."""
    try:
        import urllib.request
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "system": NORMALIZE_SCHEMA,
            "prompt": text,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 120},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
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


def safe_normalize(text: str) -> str | None:
    """Correzione STT con guardie anti-danno: accetta il testo corretto da Qwen
    solo se non fa perdere un intent keyword, un luogo o un sito noto.
    Ritorna None quando la correzione non e' utilizzabile (o identica)."""
    cand = normalize_stt(text)
    if not cand or cand.lower() == text.strip().lower():
        return None
    if "->" in cand:
        # il 0.5B a volte ricopia il formato degli esempi dello schema
        print(f"[normalize] scartata (fuga di formato): {cand!r}")
        return None
    raw_l, cand_l = text.lower(), cand.lower()
    kw_raw, kw_new = keyword_intent(raw_l), keyword_intent(cand_l)
    lost_place = (any(p in raw_l for p in LUOGHI) and not any(p in cand_l for p in LUOGHI))
    lost_site = any(s in raw_l and s not in cand_l for s in SITI_NOTI)
    if (kw_raw and kw_raw != kw_new) or lost_place or lost_site:
        print(f"[normalize] scartata (perdeva intent={kw_raw}, luogo o sito): {cand!r}")
        return None
    return cand


def ollama_parse(text: str) -> dict | None:
    """Chiede al piccolo modello locale di tradurre il comando in JSON."""
    try:
        import urllib.request
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "system": OLLAMA_SCHEMA,
            "prompt": text,
            "format": "json",
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 150},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
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
    # 2) poi le app, le cartelle e il resto
    for label, words in KEYWORDS:
        for w in words:
            if w in t:
                return label
    if "ore" in t or "ora" in t:
        return "time"
    return None


def detect_intent(text: str):
    """Laya propone, le parole chiave confermano. Ritorna (label, source)."""
    kw = keyword_intent(text)
    if kw:
        return kw, "keyword"
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

    if intent == "open_app":
        rest = re.sub(r"^(apri|lancia|avvia)\s+(il\s+|la\s+|lo\s+|l'|un\s+|una\s+)?", "", t).strip(" .!?")
        for alias, cmd in APP_ALIAS.items():
            if alias in rest or rest.startswith(alias):
                try:
                    if isinstance(cmd, list):
                        subprocess.Popen(cmd, shell=True)
                    else:
                        os.startfile(cmd)  # noqa: S606 - intenzionale, comando utente
                    return f"Sto aprendo {alias}."
                except Exception:
                    continue
        # 1) libreria indicizzata: .lnk, app Store/AppX e portabili
        hits = appindex.search(rest, limit=3)
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
            if sugg:
                with _log_lock:
                    _pending["app"] = sugg["name"]
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
        # 2) vecchio percorso: scorciatoie Start via cache .lnk
        lnk = _find_shortcut(rest)
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
                        subprocess.Popen([target], shell=True)
                        return f"Sto aprendo {lnk.stem}."
                except Exception:
                    pass
        # 3) eseguibile nel PATH (notepad, calc...)
        exe = shutil.which(rest) or shutil.which(rest + ".exe")
        if exe:
            subprocess.Popen([exe])
            return f"Sto aprendo {rest}."
        # 4) ultimo tentativo: ShellExecute risolve anche gli App Paths del registro
        try:
            os.startfile(rest)  # noqa: S606
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
            os.startfile(url)  # noqa: S606 - apre il browser predefinito (es. Comet)
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
                # os.startfile usa ShellExecute -> apre il BROWSER PREDEFINITO
                os.startfile(url)  # noqa: S606
                return f"Apro {alias} nel browser."
        m = re.search(r"(?:vai (?:su|a)|portami su|apri)\s+([a-z0-9\.\-]+\.[a-z]{2,})", t)
        if m:
            url = "https://" + m.group(1)
            os.startfile(url)  # noqa: S606
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

    if intent == "volume":
        try:
            import comtypes
            from ctypes import cast, POINTER
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            comtypes.CoInitialize()
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
            vol = cast(interface, POINTER(IAudioEndpointVolume))
            if "muto" in t or "mute" in t:
                new_mute = not bool(vol.GetMute())
                vol.SetMute(int(new_mute), None)
                return "Audio escluso." if new_mute else "Audio riattivato."
            cur = int(vol.GetMasterVolumeLevelScalar() * 100)
            if "alza" in t or "aumenta" in t:
                new = min(100, cur + 10)
            else:
                new = max(0, cur - 10)
            vol.SetMasterVolumeLevelScalar(new / 100, None)
            return f"Volume portato al {new} per cento."
        except Exception as exc:
            print(f"[volume] errore pycaw: {exc}; uso fallback tasti")
        if "muto" in t or "mute" in t:
            key = "{VK_VOLUME_MUTE}"
        elif "alza" in t or "aumenta" in t:
            key = "{VK_VOLUME_UP}"
        else:
            key = "{VK_VOLUME_DOWN}"
        ps = (f"$w=New-Object -ComObject WScript.Shell; "
              f"1..10 | ForEach-Object {{ $w.SendKeys('{key}') }}")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        if "muto" in t:
            return "Comando muto inviato."
        return "Volume regolato."

    if intent == "list_files":
        loc = extract_location(text)
        try:
            items = sorted(p.name for p in loc.iterdir())
        except Exception:
            return f"Non riesco a leggere la cartella {loc}."
        preview = ", ".join(items[:12])
        more = f" e altri {len(items) - 12}" if len(items) > 12 else ""
        return f"In {loc.name} trovo: {preview}{more}."

    return ("Non ho capito il comando. Posso creare o eliminare cartelle, aprire app e "
            "siti, darti ora e data, regolare il volume o elencare i file.")


# ---------------------------------------------------------------------------
# Pipeline completa
# ---------------------------------------------------------------------------
# Conferma vocale delle correzioni 'molto diverse': se Qwen riscrive la
# trascrizione radicalmente, Chicco chiede 'Hai detto ...?' ed esegue solo
# dopo un si' vocale (o annulla con no). Scopo dopo PENDING_TTL secondi.
CONFIRM_RATIO = 0.55
PENDING_TTL = 90.0
_pending = {"text": None, "app": None, "ts": 0.0}
_YES = {"si", "sì", "ok", "okay", "confermo", "conferma", "esatto", "esatta",
        "giusto", "giusta", "certo", "certamente", "appunto", "sicuro",
        "corretto", "corretta", "esegui", "vai", "yes", "sure", "quoto"}
_NO = {"no", "nope", "annulla", "annullare", "cancella", "sbagliato",
       "sbagliata", "falso", "falsa", "riprova", "stop", "negativo",
       "non", "niente", "mica"}


def _yes_no(text: str):
    """True (affermazione), False (negazione) o None (non e' una risposta).
    Tollerante ai near-miss dello STT ('confirmo' -> 'confermo')."""
    tokens = re.findall(r"[a-zà-ù]+", (text or "").lower())
    if not tokens:
        return None
    tset = set(tokens)
    if tset & _NO:
        return False
    if tset & _YES:
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
            "model": OLLAMA_MODEL,
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
        with urllib.request.urlopen(req, timeout=15) as r:
            out = (json.loads(r.read().decode()).get("response") or "").strip().strip('"').strip()
        if not out or "\n" in out or len(out) > 60:
            return None
        low = out.lower()
        for app in appindex.search(out, limit=5):
            if app["name"].lower() == low and app["name"] in cand_names:
                return app  # valido solo se e' ESATTAMENTE uno dei candidati
        return None
    except Exception as exc:
        print(f"[open_app] qwen_app_suggest errore: {exc}")
        return None


def _emit(user: str, reply: str, intent: str, src: str, source: str, dt: int,
          raw: str | None = None, show_list: bool = False) -> dict:
    """Costruisce l'entry di risposta: history, log, voce e ritorno API."""
    entry = {
        "user": user, "assistant": reply, "intent": intent,
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
    print(f"[cmd] intent={intent} via {src} ({dt} ms): {user!r} -> {reply!r}")
    try:
        speak(reply)
    except Exception as exc:
        print(f"[tts] errore: {exc}")
    return entry


def process(text: str, source: str) -> dict:
    raw_stt, corrected = text, None
    # --- conferma in attesa ('si' esegue, 'no' annulla) ---
    # NOTA: il controllo va SEMPRE prima della normalizzazione Qwen, che
    # riscriverebbe 'si confermo' in 'Conferma.' mandando in crash la logica
    if _yes_no(text or "") is not None:
        decision = _yes_no(text)
        with _log_lock:
            pend_text, pend_app, pend_ts = _pending["text"], _pending["app"], _pending["ts"]
            _pending["text"] = _pending["app"] = None
        if pend_ts and time.time() - pend_ts <= PENDING_TTL:
            if decision is True and (pend_app or pend_text):
                if pend_app:  # 'intendavi X?' confermato: avvia l'app
                    print(f"[confirm] app confermata, avvio: {pend_app!r}")
                    reply = run_command(f"apri {pend_app}", "open_app")
                    return _emit(f"apri {pend_app}", reply, "open_app", "guard", source, 0)
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
                return _emit(text or "no", "Va bene, annullato.", "confirm", "guard", source, 0)
        # si/no ma nessuna conferma valida in attesa: prosegui come nuovo comando
    else:
        with _log_lock:  # un nuovo comando fa decadere eventuali conferme
            _pending["text"] = _pending["app"] = None

    if text:
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
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le",
             "-ac", "1", "-ar", "16000", "pipe:1"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        )
    except FileNotFoundError:
        return JSONResponse({"error": "ffmpeg non trovato: installalo per lo STT dal browser"},
                            status_code=500)
    return _handle_pcm(proc.stdout)


@app.post("/api/listen_wav")
async def api_listen_wav(request: Request):
    """Riceve un WAV PCM (widget desktop), lo trascrive con Vosk ed esegue (senza ffmpeg)."""
    data = await request.body()
    try:
        return _handle_pcm(_wav_to_pcm16k(data))
    except Exception as exc:
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


def _handle_pcm(pcm: bytes):
    if not pcm:
        return JSONResponse({"error": "audio vuoto"}, status_code=400)
    text = transcribe(pcm)
    if not text:
        entry = {"user": "", "assistant": "Non ho sentito nulla, riprova.",
                 "intent": "-", "detector": "-", "input": "voce", "ms": 0,
                 "ts": datetime.now().isoformat(timespec="seconds")}
        with _log_lock:
            _history.append(entry)
        speak(entry["assistant"])
        return entry
    return process(text, "voce")


@app.post("/api/text")
async def api_text(payload: dict):
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "testo vuoto"}, status_code=400)
    return process(text, "testo")


@app.post("/api/normalize")
def api_normalize(payload: dict):
    """Corregge una trascrizione STT con Qwen (guardie incluse), SENZA eseguire
    nulla: per mostrare nel widget 'cosa ho sentito vs cosa ho capito'."""
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "testo vuoto"}, status_code=400)
    corrected = safe_normalize(text)
    return {"raw": text, "text": corrected or text, "corrected": bool(corrected)}


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


@app.get("/api/stt")
def api_stt():
    """Quale trascrittore e' attivo (Whisper GPU o fallback Vosk)."""
    if whisper_available():
        device = "vulkan/gpu"
        try:
            import transcribe_cpp as tc
            devs = [str(b).lower() for b in getattr(tc, "backends", [])]
            if not any("vulkan" in d or "cuda" in d or "rocm" in d for d in devs):
                device = "cpu"
        except Exception:
            pass
        return {"engine": "whisper", "model": "large-v3-turbo Q8_0", "device": device}
    return {"engine": "vosk", "model": "small-it-0.22", "device": "cpu"}


@app.get("/_tts_reply.wav")
def tts_wav():
    """Ultima risposta vocale generata, riprodotta dalla UI."""
    return FileResponse(BASE / "_tts_reply.wav", media_type="audio/wav")


@app.get("/api/history")
def api_history():
    with _log_lock:
        return {"history": list(reversed(_history[-50:]))}


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
