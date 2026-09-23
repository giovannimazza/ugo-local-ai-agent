# -*- coding: utf-8 -*-
"""
TTS neurale locale con Piper (voce italiana Paola).

Piper e' un sintetizzatore neurale offile, velocissimo su CPU: la voce
"Paola" (it_IT, medium ~63 MB) suona naturale, senza il timbro robotico
delle voci SAPI. Tutto vive in %LOCALAPPDATA%/chicco (fuori dalla repo):

  chicco/piper/            binario piper (scaricato al primo uso, ~21 MB)
  chicco/piper_voices/     modello voce + config
  chicco/tts_engine.json   motore scelto dall'utente ("piper" | "sapi")

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
VOICE_KEY = "it_IT-paola-medium"
VOICE_ONNX = VOICES_DIR / f"{VOICE_KEY}.onnx"
VOICE_JSON = VOICES_DIR / f"{VOICE_KEY}.onnx.json"
VOICE_ONNX_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
                  "it/it_IT/paola/medium/it_IT-paola-medium.onnx")
VOICE_JSON_URL = VOICE_ONNX_URL + ".json"
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


def is_ready() -> bool:
    """True se binario + voce sono completi (o gia' usati con successo)."""
    if _marker_ok(PIPER_DIR / ".done", _asset) and _marker_ok(VOICES_DIR / ".done", VOICE_KEY):
        return True
    return piper_exe().exists() and VOICE_ONNX.exists() and VOICE_JSON.exists()


def status() -> dict:
    return {"piper_ready": is_ready(), "downloading": _state["downloading"],
            "voice": VOICE_KEY}


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


def _install_voice() -> None:
    if not VOICE_ONNX.exists():
        _download(VOICE_ONNX_URL, VOICE_ONNX)
    if not VOICE_JSON.exists():
        _download(VOICE_JSON_URL, VOICE_JSON)
    _write_marker(VOICES_DIR / ".done", VOICE_KEY)


def download_async() -> threading.Thread:
    """Scarica binario + voce in background (idempotente)."""
    def work():
        with _lock:
            if is_ready():
                return
            _state["downloading"] = True
        try:
            print("[piper] scarico la voce naturale (Piper ~21 MB + Paola ~63 MB)...")
            if not _marker_ok(PIPER_DIR / ".done", _asset) or not piper_exe().exists():
                _install_binary()
            if not VOICE_ONNX.exists() or not VOICE_JSON.exists():
                _install_voice()
            print("[piper] voce naturale pronta: da ora risponde Paola.")
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
        if is_ready():
            return True
        try:
            print("  download Piper (binario ~21 MB + voce Paola ~63 MB)...")
            if not piper_exe().exists():
                _install_binary()
            if not VOICE_ONNX.exists() or not VOICE_JSON.exists():
                _install_voice()
            return is_ready()
        except Exception as exc:
            print(f"  [piper] installazione non riuscita ({exc}); il server riprovera' in background")
            return False


def synthesize(text: str, out_wav: Path) -> bool:
    """Sintetizza text su out_wav (16-bit WAV). False se Piper non e' utilizzabile."""
    if get_engine() != "piper" or not is_ready():
        return False
    cmd = [str(piper_exe()), "-m", str(VOICE_ONNX), "-f", str(out_wav),
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
