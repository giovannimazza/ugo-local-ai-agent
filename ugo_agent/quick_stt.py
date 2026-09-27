# -*- coding: utf-8 -*-
"""
STT leggero SENZA NeMo: sherpa-onnx + modello Cohere Transcribe 14 lingue
(int8, ~1.7 GB, encoder/decoder ONNX, inferenza CPU, NON-streaming).

Perche' esiste: il runtime NeMo toolkit (extra [nemotron]) richiede Python
3.10-3.12: su 3.13+ non si installa e la cascata STT degrada su
faster-whisper ovunque. Questo modulo offre la STESSA interfaccia di
nemotron_stt (available/ready/download_async/transcribe_pcm) ma su runtime
ONNX puro: sherpa-onnx ha wheel ufficiali per 3.10-3.14 (Windows/macOS/
Linux) e il modello supporta italiano e inglese (14 lingue in totale,
punteggiatura e numeri inclusi).

Si attiva con:

    pip install "ugo-agent[quick]"     (oppure: pip install sherpa-onnx)

Il modello viene scaricato una volta sola (GitHub releases, bundle
tar.bz2) nella cartella dati. Se sherpa-onnx o il modello non sono
disponibili, il server ricade su faster-whisper come sempre: mai bloccare
la pipeline. Env UGO_QUICKSTT=0 disattiva tutto.
"""
import os
import threading
import urllib.request

from . import platform_utils as _pu

BASE = _pu.data_dir()
MODELS_DIR = BASE / "models"
# Bundle ufficiale dello sherpa-onnx release "asr-models": contiene la
# cartella sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01/ con
# encoder.int8.onnx, decoder.int8.onnx e tokens.txt
MODEL_NAME = "sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01"
MODEL_URL = os.environ.get("UGO_QUICKSTT_URL", (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    + MODEL_NAME + ".tar.bz2"))
MODEL_DIR = MODELS_DIR / MODEL_NAME

_ENC = "encoder.int8.onnx"
_DEC = "decoder.int8.onnx"
_TOK = "tokens.txt"

_lock = threading.Lock()
_state = {"downloading": False, "pct": 0}
_model = None  # caricato lazy, una sola volta


def _env_enabled() -> bool:
    return os.environ.get("UGO_QUICKSTT", "1") != "0"


def available() -> bool:
    """sherpa-onnx importabile e funzione non disattivata via env."""
    if not _env_enabled():
        return False
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:
        return False


def ready() -> bool:
    """I tre file del modello sono gia' estratti nella cartella dati."""
    try:
        return all((MODEL_DIR / f).is_file() for f in (_ENC, _DEC, _TOK))
    except Exception:
        return False


def status() -> dict:
    return {"available": available(), "ready": ready(),
            "downloading": _state["downloading"], "pct": _state["pct"],
            "model": MODEL_NAME}


def download_async() -> threading.Thread | None:
    """Scarica ed estrae il bundle modello in background (idempotente)."""
    if ready() or _state["downloading"] or not available():
        return None

    def work():
        with _lock:
            if ready():
                return
            _state["downloading"] = True
            _state["pct"] = 0
        tmp = MODELS_DIR / (MODEL_NAME + ".tar.bz2.part")
        done = MODELS_DIR / (MODEL_NAME + ".tar.bz2")
        try:
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            print(f"[quick-stt] scarico {MODEL_NAME} (~1.7 GB, una volta sola)...")
            with urllib.request.urlopen(MODEL_URL, timeout=60) as r, \
                    open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0) or 1
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    _state["pct"] = min(99, int(got * 100 / total))
            tmp.rename(done)
            print("[quick-stt] estrazione...")
            import tarfile
            with tarfile.open(done, "r:bz2") as tf:
                tf.extractall(MODELS_DIR)  # noqa: S202 - bundle firmato k2-fsa
            done.unlink(missing_ok=True)
            if ready():
                _state["pct"] = 100
                print("[quick-stt] modello pronto.")
            else:
                print("[quick-stt] estrazione incompleta: uso faster-whisper.")
        except Exception as exc:
            print(f"[quick-stt] download non riuscito ({exc}); "
                  "la trascrizione restera' su faster-whisper.")
            tmp.unlink(missing_ok=True)
        finally:
            _state["downloading"] = False

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def _get_model():
    """Carica il riconoscitore una sola volta (thread-safe)."""
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is None:
            if not (available() and ready()):
                return None
            import sherpa_onnx
            print("[quick-stt] carico Cohere Transcribe 14 lingue (int8)...")
            _model = sherpa_onnx.OfflineRecognizer.from_cohere_transcribe(
                encoder=str(MODEL_DIR / _ENC),
                decoder=str(MODEL_DIR / _DEC),
                tokens=str(MODEL_DIR / _TOK),
                debug=False)
    return _model


def _lang() -> str:
    """Lingua di trascrizione: segue l'impostazione globale (bandiera UI)."""
    try:
        from . import piper_tts
        return piper_tts.current_lang()
    except Exception:
        return os.environ.get("WHISPER_LANG", "it") or "it"


def transcribe_wav(wav_path) -> str:
    """Trascrive un file WAV 16 kHz mono. Ritorna '' se non utilizzabile:
    il chiamante ricade su Whisper (mai bloccare dettatura ne' comandi)."""
    if not _env_enabled():
        return ""
    try:
        import wave as _wave
        import numpy as _np
        m = _get_model()
        if m is None:
            return ""
        with _wave.open(str(wav_path), "rb") as w:
            sr = w.getframerate()
            frames = w.readframes(w.getnframes())
        a = _np.frombuffer(frames, dtype=_np.int16).astype(_np.float32) / 32768.0
        if a.size == 0:
            return ""
        stream = m.create_stream()
        try:
            stream.set_option("language", _lang())
        except Exception:
            pass  # lingua automatica se l'API non la espone
        stream.accept_waveform(sr, a)
        m.decode_stream(stream)
        return (stream.result.text or "").strip()
    except Exception as exc:
        print(f"[quick-stt] trascrizione non riuscita ({exc}); fallback Whisper")
        return ""


def transcribe_pcm(pcm16: bytes) -> str:
    """PCM s16le 16 kHz mono -> testo (per la trascrizione dei comandi):
    scrive un WAV temporaneo e usa lo stesso motore della dettatura.
    '' se non utilizzabile -> il server usa la cascata successiva."""
    if not _env_enabled() or not pcm16:
        return ""
    tmp = BASE / "_quickstt_tmp.wav"
    try:
        import wave as _wave
        m = _get_model()
        if m is None:
            return ""
        with _wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm16)
        return transcribe_wav(tmp)
    except Exception as exc:
        print(f"[quick-stt] transcribe_pcm non riuscito ({exc})")
        return ""
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
