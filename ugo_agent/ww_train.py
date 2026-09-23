# -*- coding: utf-8 -*-
"""
Addestramento del modello di wake word "Ugo" per openWakeWord.

Dataset SINTETICO con le voci Piper (paola/riccardo it + lessac/amy en):
clip da 3 s con la wake word (o varianti: sugo, hugo...) che termina
verso la fine, piu' comandi/frasi/rumore come negativi. Embeddings del
modello ufficiale OWW (L2-normalizzati per frame), MLP 2 strati, export
ONNX nel formato openwakeword.Model: input (N, 16, 96), output (N, 1).

La SOGLIA viene scelta sulla validazione STREAMING (le finestre scorrono
sull'audio come nel widget) e il patience consigliato e' testato qui.

Uso:  python -m ugo_agent.ww_train [--rigenera]
Esito: %LOCALAPPDATA%/chicco/ww_ugo.onnx (+ .json di metadati)
"""
import json
import os
import random
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

try:
    from scipy.signal import resample_poly
except ImportError:
    sys.exit("serve scipy:  pip install scipy")

try:
    from . import platform_utils as _pu
except ImportError:  # importato come modulo top-level
    import platform_utils as _pu

BASE = _pu.data_dir()
PIPER = BASE / "piper" / "piper" / ("piper.exe" if os.name == "nt" else "piper")
OUT = Path.home() / ".cache" / "ww_ugo"
CLIP = 3 * 16000
SR = 16000
SEED = 20260923
WORD_END_MIN, WORD_END_MAX = 2.50, 2.92

COMMANDS = ["che ore sono", "apri spotify", "apri steam", "metti il volume a trenta",
            "crea una cartella sul desktop", "che tempo fa oggi", "chiudi la finestra",
            "scrivi nella lista della spesa", "apri youtube", "dammi l'ora"]
POS_WORDS = ["Ugo", "Ugo?", "Ehi Ugo", "Oh Ugo", "Su, Ugo", "Dai Ugo", "Ugo sei li",
             "Sugo", "Hugo"]
NEG_WORDS = ["luca", "cuoco", "tuono", "giugno", "duro", "otto", "tavolo",
             "musica", "finestra"]
NEG_SENTENCES = ["buongiorno a tutti quanti", "oggi e' stata una giornata lunga",
                 "dammi un consiglio per cena", "ricordami la riunione di domani",
                 "che giorno e' oggi", "domani vado a fare la spesa",
                 "il treno parte alle nove", "stasera guardiamo un film",
                 "il gatto dorme sul divano", "questo panino era squisito"]
EN_NEG = ["hey jarvis open the door", "what time is it in london",
          "alexa play some music", "could you help me with this report",
          "the weather is lovely today", "remind me to call john tomorrow",
          "i would like a large coffee please", "turn off the living room lights"]

_NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _voices():
    extra = Path.home() / ".cache" / "piper_extra"
    v = [(BASE / "piper_voices" / "it_IT-paola-medium.onnx", 0.9),
         (BASE / "piper_voices" / "it_IT-paola-medium.onnx", 1.05),
         (extra / "it_IT-riccardo-x_low.onnx", 0.95),
         (extra / "en_US-lessac-medium.onnx", 0.95),
         (extra / "en_US-amy-medium.onnx", 1.05)]
    return [(p, s) for p, s in v if p.exists()]


def _piper_batch(model: Path, lines: list[str], tag: str, length_scale: float) -> list[Path]:
    d = OUT / "raw" / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "in.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [str(PIPER), "-m", str(model), "--output_dir", str(d),
           "--length_scale", str(length_scale), "--sentence_silence", "0.05"]
    if random.random() < 0.5:
        cmd += ["--noise_scale", "0.75"]
    with open(d / "in.txt", "r", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdin=f, capture_output=True, timeout=3600, **_NOWIN)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
    return sorted(d.glob("*.wav"))


def _load_wav(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != SR:
        g = np.gcd(sr, SR)
        x = resample_poly(x, SR // g, sr // g)
    return x


def _trim(x: np.ndarray, floor: float = 0.05) -> np.ndarray:
    if x.size == 0:
        return x
    idx = np.where(np.abs(x) > np.max(np.abs(x)) * floor)[0]
    if idx.size == 0:
        return x
    m = int(0.04 * SR)
    return x[max(0, idx[0] - m): min(len(x), idx[-1] + m)]


def _clip_with_end(x: np.ndarray, rng) -> tuple[np.ndarray, float, float]:
    """Clip da 3 s con il parlato che termina verso la fine: (clip, s0, s1)."""
    if len(x) >= CLIP:
        s = rng.integers(0, len(x) - CLIP + 1)
        return x[s:s + CLIP], -1.0, -1.0
    end_s = float(rng.uniform(WORD_END_MIN, WORD_END_MAX))
    pos = int(end_s * SR) - len(x)
    if pos < 0:
        pos = CLIP - len(x)
        end_s = (pos + len(x)) / SR
    y = rng.normal(0, rng.uniform(0.0004, 0.0015), CLIP)
    y[pos:pos + len(x)] += x
    return y, pos / SR, (pos + len(x)) / SR


def _augment(y: np.ndarray, rng) -> np.ndarray:
    y = y * rng.uniform(0.45, 1.5)
    snr = rng.choice([12.0, 16.0, 20.0, 25.0, 30.0, 1e9])
    p_noise = (np.mean(y ** 2) + 1e-12) / snr
    y = y + rng.normal(0, np.sqrt(p_noise), y.shape)
    return np.clip(y + rng.normal(0, 1e-4), -1, 1)


def _noise_clip(rng) -> np.ndarray:
    t = np.arange(CLIP) / SR
    kind = rng.integers(0, 3)
    if kind == 0:
        y = rng.normal(0, rng.uniform(0.002, 0.02), CLIP)
    elif kind == 1:
        y = (0.01 * np.sin(2 * np.pi * rng.choice([50, 100]) * t)
             + 0.006 * np.sin(2 * np.pi * 120 * t + rng.uniform(0, 6)))
        y += rng.normal(0, 0.002, CLIP)
    else:
        env = 0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(0.5, 3) * t + rng.uniform(0, 6))
        y = env * rng.normal(0, 0.008, CLIP)
    return np.clip(y, -1, 1)


def _build_audio() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Genera le clip (o le ricarica dalla cache): ritorna (audio, span, yclip)."""
    ca, cs, cy = OUT / "audio.npy", OUT / "span.npy", OUT / "yclip.npy"
    if ca.exists() and cs.exists() and cy.exists():
        print("== 1. dataset in cache ==")
        return np.load(ca), np.load(cs), np.load(cy)
    rng = np.random.default_rng(SEED)
    random.seed(SEED)
    voices = _voices()
    if not voices or not PIPER.exists():
        sys.exit("Piper o le voci non sono disponibili: esegui prima ugo run")
    (OUT / "raw").mkdir(parents=True, exist_ok=True)

    print("== 1. generazione clip (Piper) ==")
    pos_clips, pos_span = [], []
    for vi, (model, ls) in enumerate(voices):
        lines = [p for p in (POS_WORDS * 16)]
        wavs = _piper_batch(model, lines, f"pos{vi}", ls)
        for wv in wavs:
            x = _trim(_load_wav(wv))
            if rng.random() < 0.35:   # velocita' diversa = 'parlante' sintetico nuovo
                f = float(rng.uniform(0.9, 1.12))
                g = np.gcd(int(f * 100), 100)
                x = resample_poly(x, int(f * 100) // g, 100 // g)
            c, s0, s1 = _clip_with_end(x, rng)
            pos_clips.append(c)
            pos_span.append((s0, s1))
            wv.unlink()
        print(f"  positive {model.stem}: {len(wavs)} clip")

    neg_clips, neg_span = [], []
    for vi, (model, ls) in enumerate(voices[:4]):
        if "en_US" in model.stem:
            lines = [s for s in EN_NEG for _ in range(12)]
        else:
            lines = ([w for w in NEG_WORDS for _ in range(12)]
                     + [c for c in COMMANDS for _ in range(4)]
                     + [s for s in NEG_SENTENCES for _ in range(4)])
        wavs = _piper_batch(model, lines, f"neg{vi}", ls)
        for wv in wavs:
            x = _load_wav(wv)
            if len(x) > 1.2 * SR:
                end = int(rng.uniform(WORD_END_MIN, WORD_END_MAX) * SR)
                s = max(0, end - len(x))
                x = x[s:s + CLIP] if len(x) > CLIP else np.pad(x, (CLIP - len(x), 0))
                neg_clips.append(x)
                neg_span.append((-1.0, -1.0))
            else:
                c, s0, s1 = _clip_with_end(_trim(x), rng)
                neg_clips.append(c)
                neg_span.append((s0, s1))
            wv.unlink()
        print(f"  negativi {model.stem}: {len(wavs)} clip")
    for i in range(150):
        neg_clips.append(_noise_clip(rng))
        neg_span.append((-1.0, -1.0))
    print(f"  clip di rumore: 150")

    Xa, spana, yclip = [], [], []
    for c, sp in zip(pos_clips, pos_span):
        for _ in range(3):
            Xa.append(_augment(c.copy(), rng))
            spana.append(sp)
            yclip.append(1)
    for c, sp in zip(neg_clips, neg_span):
        for _ in range(2):
            Xa.append(_augment(c.copy(), rng))
            spana.append(sp)
            yclip.append(0)
    X_audio = np.stack(Xa).astype(np.int16)
    spana = np.array(spana, dtype=np.float32)
    yclip = np.array(yclip)
    print(f"  clip totali: {len(yclip)} (positive {int(yclip.sum())})")
    np.save(ca, X_audio)
    np.save(cs, spana)
    np.save(cy, yclip)
    return X_audio, spana, yclip


def _real_audio():
    """Clip REALE registrate con ww_collect: (audio, span, y) o None.
    La cache e' invalidata quando cambia il contenuto di raw/."""
    raw = Path.home() / ".cache" / "ww_ugo_real" / "raw"
    files = sorted(raw.glob("*.wav")) if raw.exists() else []
    if not files:
        return None
    key = json.dumps([[f.name, f.stat().st_size, int(f.stat().st_mtime)] for f in files])
    ck, ca, cs, cy = (OUT / "real_key.json", OUT / "real_audio.npy",
                      OUT / "real_span.npy", OUT / "real_y.npy")
    if ck.exists() and ca.exists() and cy.exists() and ck.read_text() == key:
        return np.load(ca), np.load(cs), np.load(cy)
    print(f"== 1b. clip reali: {len(files)} file ==")
    rng = np.random.default_rng(SEED + 77)
    Xa, spana, ycl = [], [], []
    for f in files:
        x = _trim(_load_wav(f))
        if len(x) < 0.25 * SR:
            continue
        kind = 1 if f.name.startswith("pos") else 0
        for _ in range(3):
            c, s0, s1 = _clip_with_end(x, rng)
            Xa.append(_augment(c, rng))
            # span = SOLO la wake word (all'inizio del parlato nelle frasi
            # positive): le finestre con il comando che segue restano negative,
            # altrimenti 'apri spotify' sarebbe 1 nei positivi e 0 nei negativi
            spana.append((s0, min(s0 + 0.7, s1)) if kind else (-1.0, -1.0))
            ycl.append(kind)
    X = np.stack(Xa).astype(np.int16)
    spana = np.array(spana, np.float32)
    ycl = np.array(ycl)
    print(f"   clip reali con augment: {len(ycl)} (positive {int(ycl.sum())})")
    np.save(ca, X)
    np.save(cs, spana)
    np.save(cy, ycl)
    ck.write_text(key)
    return X, spana, ycl


def _embeddings(X_audio: np.ndarray) -> np.ndarray:
    ce = OUT / "emb.npy"
    if ce.exists():
        e = np.load(ce)
        if len(e) == len(X_audio):        # cache valida solo se il dataset non e' cambiato
            print("== 2. embeddings in cache ==")
            return e.astype(np.float32)
        ce.unlink()
    print("== 2. embeddings openWakeWord ==")
    from openwakeword.utils import AudioFeatures
    feats = AudioFeatures(inference_framework="onnx")
    emb = feats.embed_clips(X_audio)
    print(f"  embeddings: {emb.shape}")
    np.save(ce, emb.astype(np.float16))       # metà memoria, float32 al load
    return emb.astype(np.float32)


def _windows(emb: np.ndarray, spana: np.ndarray, yclip: np.ndarray):
    F = emb.shape[1]
    fps = (F - 1) / 3.0
    offs = list(range(0, F - 15, 2))
    Xw, yw, sid = [], [], []
    # L2-per-frame anche in training: il grafo ONNX la applica a runtime
    # (in streaming openwakeword passa gli embedding grezzi al modello)
    emb_n = emb / (np.linalg.norm(emb, axis=2, keepdims=True) + 1e-6)
    for i in range(len(emb)):
        s0, s1 = spana[i]
        for o in offs:
            Xw.append(emb_n[i, o:o + 16].reshape(-1))
            if s0 >= 0:
                w0, w1 = o / fps, (o + 16) / fps
                yw.append(1 if w0 < s1 and w1 > s0 else 0)
            else:
                yw.append(0)
            sid.append(i)
    return np.stack(Xw).astype(np.float32), np.array(yw), np.array(sid)


def main() -> int:
    rigenera = "--rigenera" in sys.argv
    if rigenera:
        for f in ("audio.npy", "span.npy", "yclip.npy", "emb.npy"):
            (OUT / f).unlink(missing_ok=True)
    X_audio, spana, yclip = _build_audio()
    real = _real_audio()
    if real is not None:
        Xr, sr_, yr = real
        n_syn = len(yclip)
        X_audio = np.concatenate([X_audio, Xr])
        spana = np.concatenate([spana, sr_])
        yclip = np.concatenate([yclip, yr])
        is_real = np.zeros(len(yclip), bool)
        is_real[n_syn:] = True
    else:
        is_real = np.zeros(len(yclip), bool)
    emb = _embeddings(X_audio)
    X, yw, sid = _windows(emb, spana, yclip)
    print(f"== 3. finestre: {len(yw)} (positive {int(yw.sum())}) ==")

    print("== 4. training (MLP 2 strati) ==")
    from sklearn.neural_network import MLPClassifier
    from sklearn.metrics import roc_auc_score
    rng2 = np.random.default_rng(SEED)
    uids = np.unique(sid)
    real_ids = uids[is_real[uids]]
    syn_ids = uids[~is_real[uids]]
    # validazione: 30% delle clip REALI (il banco di prova che conta) + 10% sintetiche
    rv = set(rng2.choice(real_ids, size=max(1, int(len(real_ids) * 0.3)),
                          replace=False).tolist()) if len(real_ids) else set()
    sv = set(rng2.choice(syn_ids, size=max(1, len(syn_ids) // 10),
                         replace=False).tolist())
    val_ids = rv | sv
    tr = ~np.isin(sid, list(val_ids))
    va = ~tr
    clf = MLPClassifier(hidden_layer_sizes=(128,), activation="relu", alpha=1e-4,
                        batch_size=512, learning_rate_init=1e-3, max_iter=40,
                        early_stopping=True, n_iter_no_change=4, random_state=SEED)
    clf.fit(X[tr], yw[tr])
    sc = clf.predict_proba(X[va])[:, 1]
    auc = roc_auc_score(yw[va], sc)
    print(f"  AUC finestre: {auc:.4f}")
    va_real = va & is_real[sid]
    if va_real.any():
        auc_real = roc_auc_score(yw[va_real], clf.predict_proba(X[va_real])[:, 1])
        print(f"  AUC finestre REALI: {auc_real:.4f}  ({int(va_real.sum())} finestre)")

    print("== 5. soglia sullo STREAMING (validazione per clip) ==")
    from openwakeword.model import Model
    # export temporaneo per la validazione
    import onnx
    from onnx import helper, TensorProto
    W1 = clf.coefs_[0].astype(np.float32).T          # (128, 1536) per Gemm transB
    b1 = clf.intercepts_[0].astype(np.float32)
    W2 = clf.coefs_[1].astype(np.float32).T          # (1, 128) per Gemm transB
    b2 = clf.intercepts_[1].astype(np.float32)
    nfeat = X.shape[1]
    graph = helper.make_graph(
        [helper.make_node("ReduceL2", ["x"], ["nrm"], axes=[2], keepdims=1),
         helper.make_node("Add", ["nrm", "eps"], ["nrm1"]),
         helper.make_node("Div", ["x", "nrm1"], ["xn"]),           # L2 per frame
         helper.make_node("Reshape", ["xn", "shape"], ["xr"]),
         helper.make_node("Gemm", ["xr", "W1", "b1"], ["h"], transB=1),
         helper.make_node("Relu", ["h"], ["hr"]),
         helper.make_node("Gemm", ["hr", "W2", "b2"], ["logits"], transB=1),
         helper.make_node("Sigmoid", ["logits"], ["output"])],
        "ugo_ww",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["N", 16, 96])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, ["N", 1])],
        [helper.make_tensor("eps", TensorProto.FLOAT, [1, 1, 1], np.array(1e-6, np.float32)),
         helper.make_tensor("W1", TensorProto.FLOAT, W1.shape, W1.flatten()),
         helper.make_tensor("b1", TensorProto.FLOAT, b1.shape, b1.flatten()),
         helper.make_tensor("W2", TensorProto.FLOAT, W2.shape, W2.flatten()),
         helper.make_tensor("b2", TensorProto.FLOAT, b2.shape, b2.flatten()),
         helper.make_tensor("shape", TensorProto.INT64, [2], [-1, nfeat])])
    m = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    m.ir_version = 8
    onnx.checker.check_model(m)
    onnx.save(m, str(OUT / "ugo_draft.onnx"))

    mm = Model(wakeword_models=[str(OUT / "ugo_draft.onnx")], inference_framework="onnx")
    key = list(mm.models.keys())[0]

    # clip di validazione con augmentazioni nuove (non viste in training)
    rng3 = np.random.default_rng(SEED + 1)
    val_idx = np.where(va)[0]
    val_clips = np.unique(sid[val_idx])
    # la soglia si sceglie sui REALI se ci sono: e' l'unico dato onesto
    pos_v = ([i for i in val_clips if yclip[i] == 1 and is_real[i]]
             or [i for i in val_clips if yclip[i] == 1])[:200]
    neg_v = ([i for i in val_clips if yclip[i] == 0 and is_real[i]]
             or [i for i in val_clips if yclip[i] == 0])[:200]
    n_posr = sum(1 for i in pos_v if is_real[i])
    n_negr = sum(1 for i in neg_v if is_real[i])
    print(f"  validazione: {len(pos_v)} positive ({n_posr} reali), "
          f"{len(neg_v)} negative ({n_negr} reali)")

    def stream_max(audio16):
        mm.reset()
        s_max = 0.0
        for i in range(0, len(audio16) - 1280 + 1, 1280):
            s_max = max(s_max, float(mm.predict(audio16[i:i + 1280]).get(key, 0.0)))
        return s_max

    pos_sc = np.array([stream_max(_augment(X_audio[i].astype(np.float32) / 32768.0,
                                           rng3).astype(np.int16)) for i in pos_v])
    neg_sc = np.array([stream_max(_augment(X_audio[i].astype(np.float32) / 32768.0,
                                           rng3).astype(np.int16)) for i in neg_v])
    # soglia: FPR ~0 con margine, poi TPR ottenuta
    th = float(np.max(neg_sc)) + 0.01
    th = min(max(th, 0.5), 0.99)
    tpr = float(np.mean(pos_sc >= th))
    print(f"  score positivi: mediana {np.median(pos_sc):.3f}, p10 {np.quantile(pos_sc, 0.1):.3f}, max {pos_sc.max():.3f}")
    print(f"  score negativi: max {neg_sc.max():.3f}, p99 {np.quantile(neg_sc, 0.99):.3f}")
    print(f"  soglia scelta: {th:.3f} | TPR streaming: {tpr:.2%} | FPR streaming: 0.00%")
    # simulo il patience del widget: 2 predizioni consecutive sopra soglia
    def stream_frames(audio16):
        mm.reset()
        return [float(mm.predict(audio16[i:i + 1280]).get(key, 0.0))
                for i in range(0, len(audio16) - 1280 + 1, 1280)]
    pat_tpr = np.mean([max(f[j] >= th and f[j + 1] >= th
                           for j in range(len(f) - 1))
                       for f in (stream_frames(_augment(X_audio[i].astype(np.float32) / 32768.0, rng3)
                                                .astype(np.int16)) for i in pos_v[:80])])
    print(f"  TPR con patience=2 (2 frame di fila): {pat_tpr:.2%}")

    print("== 6. installazione ==")
    dst = BASE / "ww_ugo.onnx"
    shutil.copy(OUT / "ugo_draft.onnx", dst)
    (BASE / "ww_ugo.json").write_text(json.dumps(
        {"threshold": round(th, 3), "stream_tpr": round(tpr, 3),
         "stream_tpr_patience2": round(float(pat_tpr), 3), "stream_fpr": 0.0,
         "auc_windows": round(float(auc), 3),
         "auc_windows_real": round(float(auc_real), 3) if va_real.any() else None,
         "clips": len(yclip), "real_clips": int(is_real.sum()),
         "voices": [p.stem for p, _ in _voices()]}, indent=1))
    print(f"  modello installato: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
