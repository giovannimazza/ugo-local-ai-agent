# -*- coding: utf-8 -*-
"""
Collettore guidato di audio REALE per il modello di wake word "Ugo".

Perché serve: i modelli OWW ufficiali sono addestrati su centinaia di migliaia
di parlanti reali; con i soli 3-5 parlanti sintetici Piper il modello non
separa bene (AUC ~0.74). 30-40 "Ugo" della tua voce + negativi reali cambiano
completamente il modello.

Uso:
    python -m chicco_agent.ww_collect

Flusso (tutto nel terminale):
    1. countdown 3-2-1 -> registra 2 s dal microfono predefinito
    2. controllo RMS: se silenzio/clipping ti chiede di ripetere
    3. salvataggio in ~/.cache/ww_ugo_real/raw/ (pos_###.wav / neg_###.wav)
    4. 'completo' quando hai abbastanza clip; poi lancia ww_train
"""
import os
import sys
import wave
import time
from pathlib import Path

import numpy as np

try:
    import soundcard as sc
except ImportError:
    sys.exit("serve soundcard:  pip install soundcard")

OUT = Path.home() / ".cache" / "ww_ugo_real"
RAW = OUT / "raw"
SR = 16000
SECS = 3.0             # 3 s: margine per partire comodo e concludere la frase
TARGET_POS = 40          # varianti di "Ugo" da registrare
TARGET_NEG = 40          # negativi reali: comandi, frasi, chiacchiere
MIN_RMS = 0.006          # sotto: silenzio / troppo lontano
MAX_RMS = 0.85           # sopra: clipping
WAKE_TOKENS = ("ugo", "u go", "hugo", "sugo", "wugo", "yugo", "jugo",
               "ghigo", "uigo", "ogu")
_whisper = None


def _lev(a: str, b: str) -> int:
    """Distanza di edit (Whisper base storcia 'Ugo' in 'uga'/'oga'/'yoga')."""
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def _verify_pos(x: np.ndarray) -> tuple[bool, str]:
    """Trascrive con Whisper e controlla che ci sia davvero la wake word
    (fuzzy: distanza di edit <= 2 su ogni token)."""
    global _whisper
    try:
        if _whisper is None:
            print("  (carico Whisper per la verifica...) ", end="", flush=True)
            from faster_whisper import WhisperModel
            _whisper = WhisperModel("base", device="cpu", compute_type="int8")
            print("ok")
        segs, _ = _whisper.transcribe(x, language="it", beam_size=1,
                                      condition_on_previous_text=False)
        txt = " ".join(s.text for s in segs).strip().lower()
    except Exception as exc:
        return True, f"(verifica non disponibile: {exc})"   # non bloccare mai
    toks = "".join(c if c.isalnum() else " " for c in txt).split()
    ok = any(_lev("ugo", t) <= 2 for t in toks) if toks else False
    return ok, txt

POS = ["Ugo", "Ehi Ugo", "Oh Ugo", "Ugo sei lì?", "Sugo", "Hugo", "Ugo!",
       "Su Ugo", "Dai Ugo", "Ugo, apri Spotify"]
NEG = ["che ore sono", "apri spotify", "apri steam", "metti il volume a trenta",
       "che tempo fa oggi", "chiudi la finestra", "dammi un consiglio per cena",
       "domani vado a fare la spesa", "stasera guardiamo un film",
       "il treno parte alle nove", "musica", "finestra", "luca", "cuoco",
       "giugno", "otto", "tavolo", "sugo di pomodoro",
       "il gatto dorme sul divano", "il panino era squisito"]


def _record() -> np.ndarray:
    mic = sc.default_microphone()
    with mic.recorder(samplerate=SR) as rec:
        frames = []
        for _ in range(int(SECS / 0.1)):
            frames.append(rec.record(numframes=SR // 10).copy())
    x = np.concatenate(frames)[:, 0].astype(np.float32)
    # normalizzazione di livello leggera (uniforma distanza dal microfono)
    p = np.max(np.abs(x)) + 1e-9
    return np.clip(x * min(3.0, 0.5 / p), -1, 1) if p < 0.5 else x


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)))


def _next_idx(kind: str) -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    i = 0
    while (RAW / f"{kind}_{i:03d}.wav").exists():
        i += 1
    return i


def _save(kind: str, idx: int, x: np.ndarray) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    p = RAW / f"{kind}_{idx:03d}.wav"
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return p


def _count(kind: str) -> int:
    if not RAW.exists():
        return 0
    return len(list(RAW.glob(f"{kind}_*.wav")))


def main() -> int:
    auto = "--auto" in sys.argv      # senza input(): ciclo continuo per finestra dedicata
    print("=" * 62)
    print(" COLLETTORE VOCE REALE — wake word 'Ugo'")
    print("=" * 62)
    print(f"Obiettivo: {TARGET_POS} positive (varianti di 'Ugo') e")
    print(f"           {TARGET_NEG} negative (frasi/comandi normali).")
    print("Regole d'oro: parla come parli DAVVERO al widget — distanza")
    print("naturale dal microfono, tono normale, nessuna recita teatrale.")
    if auto:
        print("\nMODALITA AUTO: leggi la frase mostrata e dilla durante il")
        print("countdown; la registrazione parte da sola. Ctrl+C per fermarti.")
    else:
        print("Premi INVIO e parla durante i 2 secondi di registrazione.\n")

    while True:
        np_ = _count("pos")
        nn = _count("neg")
        if np_ >= TARGET_POS and nn >= TARGET_NEG:
            print("\nCOMPLETO! Chiudi questa finestra e lancia:")
            print("  python -m chicco_agent.ww_train")
            return 0
        kind = "pos" if np_ < TARGET_POS else "neg"
        script = POS[np_ % len(POS)] if kind == "pos" else NEG[nn % len(NEG)]
        target = TARGET_POS if kind == "pos" else TARGET_NEG
        idx = np_ if kind == "pos" else nn
        print(f"\n[{kind.upper()} {idx}/{target}]  >>>  \u00ab{script}\u00bb  <<<")
        if not auto:
            try:
                input("  INVIO per registrare, 's' per saltare, 'q' per uscire > ")
            except (EOFError, KeyboardInterrupt):
                print("\nInterrotto: riprendi con  python -m chicco_agent.ww_collect")
                return 1
        time.sleep(0.2)
        print("  3..."); time.sleep(0.5)
        print("  2..."); time.sleep(0.5)
        print("  1..."); time.sleep(0.5)
        print("  🎙 DICI ORA (2 s)")
        try:
            x = _record()
        except Exception as exc:
            print(f"  ERRORE microfono: {exc}")
            return 1
        r = _rms(x)
        peak = float(np.max(np.abs(x)))
        print(f"  livello rms={r:.3f} picco={peak:.2f}")
        if r < MIN_RMS:
            print("  ⚠ silenzio o troppo lontano: ripeti (scartata, non salvata)")
            continue
        if peak > MAX_RMS:
            print("  ⚠ audio saturo: allontanati un poco, ripeti")
            continue
        if kind == "pos":
            ok, heard = _verify_pos(x)
            print(f"  whisper: {heard!r}")
            if not ok:
                print("  ✗ NON sento la wake word: avvicinati, guarda il microfono "
                      "e ripeti (scartata, non salvata)")
                continue
        p = _save(kind, _next_idx(kind), x)
        tot = _count(kind)
        print(f"  ✓ salvata {p.name}  (totale {tot}/"
              f"{TARGET_POS if kind == 'pos' else TARGET_NEG})")


if __name__ == "__main__":
    sys.exit(main())
