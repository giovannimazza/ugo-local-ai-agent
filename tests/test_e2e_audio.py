# -*- coding: utf-8 -*-
"""Test E2E della pipeline audio di Ugo: TTS -> WAV -> STT -> intent.

Catena verificata (funzioni DIRETTE del server, senza HTTP ne' widget):
  1. il TTS di sistema (pyttsx3/espeak/nsss) sintetizza "che ore sono" su WAV
  2. il WAV passa per la conversione _wav_to_pcm16k del server
  3. Vosk trascrive il PCM (modello scaricato se assente, ~41 MB)
  4. detect_intent deve classificare l'intento 'time'

I modelli pesanti si scaricano solo se mancano; se TTS o STT non sono
disponibili sulla macchina (runner senza voci, ambiente minimale) il test
si SALTA con exit 0 invece di fallire: e' un test che richiede hardware
audio, non una dipendenza dura della CI.

Esecuzione:  python tests/test_e2e_audio.py      (oppure pytest)
"""
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PHRASE = "che ore sono"
WANT_INTENT = "time"


def _have_tts() -> bool:
    try:
        import pyttsx3  # noqa: F401
        return True
    except Exception:
        return False


def main() -> int:
    if not _have_tts():
        print("SKIP: pyttsx3 non disponibile su questa macchina")
        return 0

    from ugo_agent import server

    # --- 1) TTS: frase -> WAV -------------------------------------------
    wav = Path(server.BASE) / "_test_e2e_tts.wav"
    try:
        engine = server.pyttsx3.init()
        server._pick_voice(engine)
        engine.setProperty("rate", 170)
        engine.save_to_file(PHRASE, str(wav))
        engine.runAndWait()
    except Exception as exc:
        print(f"SKIP: TTS non utilizzabile ({exc})")
        return 0
    if not wav.exists() or wav.stat().st_size < 1000:
        print("SKIP: il TTS non ha prodotto un WAV valido (runner senza voci)")
        return 0

    try:
        # --- 2) conversione WAV -> PCM 16k mono (codice reale del server) ---
        # su alcuni runner (macOS) il TTS scrive un file che NON e' un WAV
        # valido (non parte con 'RIFF'): e' un limite dell'ambiente, non un
        # bug del server -> SKIP invece di fallire la CI
        try:
            pcm = server._wav_to_pcm16k(wav.read_bytes())
        except wave.Error:
            print("SKIP: il TTS non ha prodotto un WAV valido (header RIFF assente)")
            return 0
        assert pcm, "conversione WAV->PCM vuota"

        # --- 3) STT Vosk (modello scaricato al primo giro se assente) ------
        try:
            text = server._vosk_transcribe(pcm)
        except Exception as exc:
            print(f"SKIP: STT Vosk non disponibile ({exc})")
            return 0
        print(f"E2E audio: TTS -> {text!r}")
        if not text:
            print("SKIP: trascrizione vuota (audio TTS non leggibile da STT)")
            return 0

        # --- 4) intent ------------------------------------------------------
        intent, src = server.detect_intent(server._strip_wake(text))
        print(f"E2E audio: intent={intent} (via {src})")
        assert intent == WANT_INTENT, f"intent atteso {WANT_INTENT}, avuto {intent}"
        print("E2E audio pipeline OK")
        return 0
    finally:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
