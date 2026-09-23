# -*- coding: utf-8 -*-
"""
TTS neurale locale con Piper (voce italiana Paola, inglese Amy).

Piper e' un sintetizzatore neurale offline, velocissimo su CPU: le voci
"Paola" (it_IT, medium ~63 MB) e "Amy" (en_US, medium ~63 MB) suonano
naturali, senza il timbro robotico delle voci SAPI. Tutto vive nella
cartella dati (fuori dalla repo):

  piper/piper/             binario piper (scaricato al primo uso, ~21 MB)
  piper/piper_voices/      modelli voce + config
  piper/tts_engine.json    motore scelto ("piper" | "sapi") + lingua attiva

Il download parte in background allo startup del server: finche' non e'
completo si ricade automaticamente sulla voce di sistema (SAPI/Elsa),
cosi' l'assistente non resta mai muto.
"""
import json
import os
import platform
import subprocess
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path

try:
    from . import platform_utils as _pu
except ImportError:  # importato come modulo top-level (server avviato come script)
    import platform_utils as _pu

BASE = _pu.data_dir()
PIPER_DIR = BASE / "piper"
VOICES_DIR = BASE / "piper_voices"
PREFS_FILE = BASE / "tts_engine.json"

# voci per lingua: la lingua attiva seleziona il modello usato da synthesize()
VOICES = {
    "it": {"key": "it_IT-paola-medium", "label": "Paola",
           "url": ("https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
                   "it/it_IT/paola/medium/it_IT-paola-medium.onnx")},
    "en": {"key": "en_US-amy-medium", "label": "Amy",
           "url": ("https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
                   "en/en_US/amy/medium/en_US-amy-medium.onnx")},
}
DEFAULT_LANG = "it"
_LANGS = {"it", "en"}


def _voice_info(lang: str) -> dict:
    return VOICES.get(lang or DEFAULT_LANG, VOICES[DEFAULT_LANG])


def current_lang() -> str:
    """Lingua attiva del TTS (persistita; 'it' di default)."""
    lang = _load_prefs().get("lang", DEFAULT_LANG)
    return lang if lang in _LANGS else DEFAULT_LANG


def set_language(lang: str) -> None:
    """Cambia la lingua del TTS (e quindi la voce di default accoppiata)."""
    if lang not in _LANGS:
        lang = DEFAULT_LANG
    prefs = _load_prefs()
    prefs["lang"] = lang
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(prefs))
    except Exception:
        pass
    if lang != "it":
        ensure_voice(lang)  # scarica la voce EN in background se manca


def voice_key_for(lang: str) -> str:
    return _voice_info(lang)["key"]


def _voice_paths(lang: str) -> tuple[Path, Path]:
    key = voice_key_for(lang)
    return VOICES_DIR / f"{key}.onnx", VOICES_DIR / f"{key}.onnx.json"


def voice_ready(lang: str) -> bool:
    onnx, js = _voice_paths(lang)
    return onnx.exists() and js.exists()


def ensure_voice(lang: str) -> threading.Thread | None:
    """Scarica la voce della lingua data in background, se manca."""
    if voice_ready(lang) or _state["downloading"]:
        return None
    def work():
        with _lock:
            if voice_ready(lang):
                return
            _state["downloading"] = True
        try:
            info = _voice_info(lang)
            print(f"[piper] scarico la voce {info['label']} ({info['key']})...")
            onnx, js = _voice_paths(lang)
            if not onnx.exists():
                _download(info["url"], onnx)
            if not js.exists():
                _download(info["url"] + ".json", js)
            print(f"[piper] voce {info['label']} pronta.")
        except Exception as exc:
            print(f"[piper] download voce {lang} non riuscito ({exc}).")
        finally:
            _state["downloading"] = False
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t
PIPER_RELEASE = "2023.11.14-2"

_sys, _mach = platform.system(), platform.machine().lower()
if _sys == "Windows":
    _asset = "piper_windows_amd64.zip"
    _EXE_NAME = "piper.exe"
elif _sys == "Darwin":
    _asset = ("piper_macos_aarch64.tar.gz" if "arm" in _mach or "aarch" in _mach
              else "piper_macos_x64.tar.gz")
    _EXE_NAME = "piper"
else:
    _asset = ("piper_linux_aarch64.tar.gz" if "arm" in _mach or "aarch" in _mach
              else "piper_linux_x86_64.tar.gz")
    _EXE_NAME = "piper"
PIPER_URL = f"https://github.com/rhasspy/piper/releases/download/{PIPER_RELEASE}/{_asset}"

# niente finestra console a ogni sintesi (Windows)
_NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if _sys == "Windows" else {}

_lock = threading.Lock()
_state = {"downloading": False}


def _load_prefs() -> dict:
    try:
        return json.loads(PREFS_FILE.read_text()) if PREFS_FILE.exists() else {}
    except Exception:
        return {}


def get_engine() -> str:
    """Motore scelto dall'utente ('piper' di default, 'sapi' = voce di sistema)."""
    return _load_prefs().get("engine", "piper")


def set_engine(name: str) -> None:
    prefs = _load_prefs()
    prefs["engine"] = "sapi" if name == "sapi" else "piper"
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(prefs))
    except Exception:
        pass


def _marker_ok(marker: Path, key: str) -> bool:
    try:
        return marker.exists() and json.loads(marker.read_text()).get("k") == key
    except Exception:
        return False


def _write_marker(marker: Path, key: str) -> None:
    try:
        marker.write_text(json.dumps({"k": key}))
    except Exception:
        pass


def piper_exe() -> Path:
    return PIPER_DIR / "piper" / _EXE_NAME


def is_ready(lang: str | None = None) -> bool:
    """True se binario + voce della lingua (default: attiva) sono completi."""
    lang = lang or current_lang()
    return piper_exe().exists() and voice_ready(lang)


def status() -> dict:
    lang = current_lang()
    return {"piper_ready": is_ready(lang), "downloading": _state["downloading"],
            "lang": lang, "voice": voice_key_for(lang),
            "voices": {lg: voice_ready(lg) for lg in _LANGS}}


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def hook(n, bs, total):
        if total > 0 and n % 40 == 0:
            print(f"[piper] {dest.name}: {min(n * bs, total) // 1048576} MB / {total // 1048576} MB")
    urllib.request.urlretrieve(url, tmp, reporthook=hook)
    tmp.replace(dest)


def _install_binary() -> None:
    archive = PIPER_DIR / _asset
    _download(PIPER_URL, archive)
    if _asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(PIPER_DIR)
    else:
        with tarfile.open(archive) as t:
            t.extractall(PIPER_DIR)
    archive.unlink(missing_ok=True)
    if _sys != "Windows":
        piper_exe().chmod(0o755)
    if not piper_exe().exists():
        raise RuntimeError(f"binario piper non trovato dopo l'estrazione: {piper_exe()}")
    _write_marker(PIPER_DIR / ".done", _asset)


def _install_voice(lang: str) -> None:
    onnx, js = _voice_paths(lang)
    url = _voice_info(lang)["url"]
    if not onnx.exists():
        _download(url, onnx)
    if not js.exists():
        _download(url + ".json", js)
    _write_marker(VOICES_DIR / ".done", voice_key_for(lang))


def download_async() -> threading.Thread:
    """Scarica binario + voce della lingua attiva in background (idempotente)."""
    lang = current_lang()
    def work():
        with _lock:
            if is_ready(lang):
                return
            _state["downloading"] = True
        try:
            info = _voice_info(lang)
            print(f"[piper] scarico il sintetizzatore (Piper ~21 MB + {info['label']} ~63 MB)...")
            if not _marker_ok(PIPER_DIR / ".done", _asset) or not piper_exe().exists():
                _install_binary()
            if not voice_ready(lang):
                _install_voice(lang)
            print(f"[piper] voce naturale pronta: da ora risponde {info['label']}.")
        except Exception as exc:
            print(f"[piper] download non riuscito ({exc}); resta la voce di sistema.")
        finally:
            _state["downloading"] = False

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def ensure_started() -> None:
    """Allo startup: se non pronto, avvia il download in background."""
    if not is_ready() and not _state["downloading"]:
        download_async()


def install_sync() -> bool:
    """Installazione bloccante per 'ugo setup'. True se pronto alla fine."""
    with _lock:
        lang = current_lang()
        if is_ready(lang):
            return True
        try:
            info = _voice_info(lang)
            print(f"  download Piper (binario ~21 MB + voce {info['label']} ~63 MB)...")
            if not piper_exe().exists():
                _install_binary()
            if not voice_ready(lang):
                _install_voice(lang)
            return is_ready(lang)
        except Exception as exc:
            print(f"  [piper] installazione non riuscita ({exc}); il server riprovera' in background")
            return False


def synthesize(text: str, out_wav: Path, lang: str | None = None) -> bool:
    """Sintetizza text su out_wav (16-bit WAV) con la voce della lingua attiva
    (o quella richiesta). False se Piper non e' utilizzabile per quella lingua:
    il chiamante ricade sulla voce di sistema."""
    lang = lang or current_lang()
    if get_engine() != "piper" or not piper_exe().exists():
        return False
    onnx, _js = _voice_paths(lang)
    if not (onnx.exists() and _js.exists()):
        ensure_voice(lang)  # manca: scarica in background e ripiega sotto
        return False
    cmd = [str(piper_exe()), "-m", str(onnx), "-f", str(out_wav),
           "--sentence_silence", "0.25"]
    try:
        r = subprocess.run(cmd, input=text.encode("utf-8"),
                           capture_output=True, timeout=120, **_NOWIN)
        if r.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 1000:
            return True
        print(f"[piper] sintesi fallita ({r.stderr.decode(errors='replace')[:200]}); "
              f"uso la voce di sistema")
    except Exception as exc:
        print(f"[piper] errore ({exc}); uso la voce di sistema")
    return False
