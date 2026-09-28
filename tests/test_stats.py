# -*- coding: utf-8 -*-
"""Test del modulo ugo_agent.stats: registri latenze, cold-start STT,
coda di log catturati e callback di init.

Eseguibile senza dipendenze (e senza server):
    python tests/test_stats.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ugo_agent import stats  # noqa: E402


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL"), name, ("" if cond else "| " + str(extra)[:110]))
    return bool(cond)


results = []

# --- _track: finestra mobile a 50 campioni + timestamp paralleli ---
for i in range(55):
    stats._track("nemotron", float(i))
results.append(check("_track tiene gli ultimi 50 campioni",
                     len(stats._stats["nemotron"]) == 50
                     and stats._stats["nemotron"][-1] == 54.0
                     and len(stats._stats_ts["nemotron"]) == 50))

# --- cold-start: il campione preso subito dopo il boot non gonfia la media ---
stats._track("whisper", 30.0)          # primo giro: modello in caricamento
stats._track("whisper", 3.0)           # giro successivo: a regime
# la classificazione guarda il momento della CATTURA rispetto al boot:
# sposto i timestamp di cattura come sarebbero davvero (10s e 200s dopo il boot)
boot = stats._boot_ts
stats._stats_ts["whisper"] = [boot + 10.0, boot + 200.0]
e = stats._stage_entry(list(stats._stats["whisper"]),
                       list(stats._stats_ts["whisper"]), "whisper")
results.append(check("cold-start: media sui soli campioni a regime",
                     e["avg"] == 3.0 and e["cold_n"] == 1 and e["n"] == 2, e))
stats._stats_ts["whisper"] = [boot + 200.0, boot + 201.0]
e = stats._stage_entry(list(stats._stats["whisper"]),
                       list(stats._stats_ts["whisper"]), "whisper")
results.append(check("tutti a regime: nessun flag cold", "cold_n" not in e, e))

# --- coda log: ordine recenti -> vecchi ---
for i in range(5):
    stats.log_line(f"riga {i}")
lines = stats.recent_logs(3)
results.append(check("recent_logs: ultime 3, dalla piu' recente",
                     lines[0].endswith("riga 4") and lines[2].endswith("riga 2"),
                     lines))

# --- callback di init (il server le imposta all'import) ---
stats.init(lambda: False, lambda: {"engine": "nemotron", "model": "m"},
           lambda: "qwen2.5:1.5b", lambda: "piper-paola", lambda: [])
results.append(check("callback init impostate",
                     stats._cb_qwen_on() is False
                     and stats._cb_effective_stt()["engine"] == "nemotron"
                     and stats._cb_llm_model() == "qwen2.5:1.5b"))

# --- le fasi STT note sono quelle attese dalla UI ---
results.append(check("STT_STAGES copre i motori attuali",
                     set(stats.STT_STAGES) == {"vosk", "whisper", "nemotron", "quick"},
                     stats.STT_STAGES))

print()
okn = sum(1 for r in results if r)
print(f"RISULTATO: {okn}/{len(results)} test superati")
raise SystemExit(0 if okn == len(results) else 1)
