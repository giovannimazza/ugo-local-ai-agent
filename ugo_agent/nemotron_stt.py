# -*- coding: utf-8 -*-
"""
STT streaming Nemotron 3.5 (NVIDIA) per la DITTATURA di Ugo.

Modello: nvidia/nemotron-3.5-asr-streaming-0.6b (FastConformer-RNNT,
cache-aware streaming, 40 lingue con auto-rilevamento, italiano incluso).
Rispetto a Whisper offre trascrizione pensata per lo streaming; qui lo
usiamo a "frasi": ogni blocco di dettatura viene trascritto e incollato.

Tutto OPZIONALE: NeMo non e' nelle dipendenze base (e' pesante e su alcuni
Python, es. 3.14, non ha ancora wheel). Si attiva con:

    pip install "ugo-agent[nemotron]"     (oppure: pip install nemo_toolkit[asr])

Il modello (~1.2 GB) viene scaricato automaticamente al primo uso nella
cartella dati. Se NeMo o il modello non sono disponibili, il widget e il
server ricadono da soli su faster-whisper: la dettatura funziona comunque.
"""
import threading

from . import platform_utils as _pu

BASE = _pu.data_dir()

# Il modello puo' essere cambiato via env (es. una variante fine-tuned it-IT)
MODEL_ID = __import__("os").environ.get(
    "UGO_NEMOTRON_MODEL", "nvidia/nemotron-3.5-asr-streaming-0.6b")
MODEL_DIR = BASE / "models" / "nemotron-3.5-asr-streaming-0.6b"

_lock = threading.Lock()
_state = {"downloading": False, "pct": 0}
_model = None  # caricato lazy, una sola volta


def _env_enabled() -> bool:
    import os
    return os.environ.get("UGO_NEMOTRON", "1") != "0"


def available() -> bool:
    """NeMo importabile e funzione non disattivata via env."""
    if not _env_enabled():
        return False
    try:
        import nemo.collections.asr  # noqa: F401
        return True
    except Exception:
        return False


def ready() -> bool:
    """Il file .nemo del modello e' gia' scaricato."""
    if not MODEL_DIR.is_dir():
        return False
    try:
        return any(MODEL_DIR.glob("*.nemo"))
    except Exception:
        return False


def status() -> dict:
    return {"available": available(), "ready": ready(),
            "downloading": _state["downloading"], "pct": _state["pct"],
            "model": MODEL_ID}


def download_async() -> threading.Thread | None:
    """Scarica il modello da HuggingFace in background (idempotente)."""
    if ready() or _state["downloading"] or not available():
        return None

    def work():
        with _lock:
            if ready():
                return
            _state["downloading"] = True
        try:
            from huggingface_hub import snapshot_download
            print(f"[nemotron] scarico {MODEL_ID} (~1.2 GB, una volta sola)...")
            snapshot_download(repo_id=MODEL_ID, local_dir=str(MODEL_DIR),
                              allow_patterns=["*.nemo", "*.yaml", "*.json"])
            _state["pct"] = 100
            print("[nemotron] modello pronto.")
        except Exception as exc:
            print(f"[nemotron] download non riuscito ({exc}); "
                  "la dettatura usera' Whisper.")
        finally:
            _state["downloading"] = False

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def _get_model():
    """Carica il modello una sola volta (thread-safe)."""
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is None:
            if not (available() and ready()):
                return None
            from nemo.collections.asr.models import ASRModel
            nemo_file = next(MODEL_DIR.glob("*.nemo"))
            print(f"[nemotron] carico {nemo_file.name}...")
            _model = ASRModel.restore_from(str(nemo_file), map_location="cpu")
            _model.eval()
    return _model


def transcribe_wav(wav_path) -> str:
    """Trascrive un file WAV 16 kHz mono. Ritorna '' se Nemotron non e'
    utilizzabile: il chiamante ricade su Whisper (mai bloccare la dettatura)."""
    if not _env_enabled():
        return ""
    try:
        m = _get_model()
        if m is None:
            return ""
        out = m.transcribe([str(wav_path)], batch_size=1)
        if out and isinstance(out[0], (list, tuple)):
            return " ".join(str(x) for x in out[0]).strip()
        return (str(out[0]) if out else "").strip()
    except Exception as exc:
        print(f"[nemotron] trascrizione non riuscita ({exc}); fallback Whisper")
        return ""


def transcribe_pcm(pcm16: bytes) -> str:
    """PCM s16le 16 kHz mono -> testo (per la trascrizione dei comandi):
    scrive un WAV temporaneo e usa lo stesso motore della dettatura.
    '' se Nemotron non e' utilizzabile -> il server usa Whisper."""
    if not _env_enabled() or not pcm16:
        return ""
    try:
        import numpy as _np
        import soundfile as _sf
        m = _get_model()
        if m is None:
            return ""
        a = _np.frombuffer(pcm16, dtype=_np.int16).astype(_np.float32) / 32768.0
        tmp = BASE / "_nemo_tmp.wav"
        _sf.write(str(tmp), a, 16000)
        try:
            return transcribe_wav(tmp)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
    except Exception as exc:
        print(f"[nemotron] transcribe_pcm non riuscito ({exc})")
        return ""
