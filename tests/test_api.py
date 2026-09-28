# -*- coding: utf-8 -*-
"""Test E2E delle API del server Ugo.

Richiede il server attivo (`ugo run` oppure `ugo run server`):
se non risponde su http://127.0.0.1:8123 il test esce 0 con un avviso
(usa `python tests/test_multicommand.py` per i test senza server).

Nota: i test che aprono app/siti (multi-comando) agiscono davvero sul PC.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("UGO_TEST_BASE", "http://127.0.0.1:8123")
# I test 'pesanti' agiscono davvero sul PC (volume, browser, cartelle sul
# desktop): in CI (UGO_TEST_HEAVY=0) si saltano e restano i controlli leggeri.
HEAVY = os.environ.get("UGO_TEST_HEAVY", "1") == "1"


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/api/stt", timeout=3):
            return True
    except Exception:
        return False


if not _server_up():
    print("Server non attivo su 127.0.0.1:8123: avvia 'ugo run' e rilancia.")
    sys.exit(0)

results = []


def _retry(fn, *a, **kw):
    """Il teardown COM di pycaw puo' resettare sporadicamente una connessione
    keep-alive: ritenta, il server resta sano (si vede nei suoi log)."""
    last = None
    for _ in range(3):
        try:
            return fn(*a, **kw)
        except (ConnectionResetError, TimeoutError) as exc:
            last = exc
            time.sleep(0.6)
    raise last


def get(path, timeout=20):
    def once():
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    return _retry(once)


def post(path, payload, timeout=120):
    def once():
        req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    return _retry(once)


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("OK  " if cond else "FAIL"), name, ("" if cond else "| " + str(extra)[:110]))


def _ollama_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3):
            return True
    except Exception:
        return False


# I test che passano dal modello LLM (chat, correzione) richiedono Ollama:
# in CI (o a PC senza Ollama) vengono SALTATI invece di fallire.
LLM_UP = _ollama_up()


def skip(name, why="Ollama non attivo"):
    print(f"SKIP {name} | {why}")


# --- endpoint GET ---
home = urllib.request.urlopen(BASE + "/", timeout=10).read().decode("utf-8", "replace")
check("GET / (web UI servita)", "ugo" in home.lower() and len(home) > 5000)
check("GET /api/stt", "engine" in get("/api/stt") or "model" in get("/api/stt"))
apps = get("/api/apps")
n_apps = len(apps.get("apps", apps.get("items", [])))
check("GET /api/apps (libreria indicizzata)", n_apps > 10, f"apps={n_apps}")
stats = get("/api/stats")
check("GET /api/stats", "stages" in stats and "models" in stats, list(stats))
check("GET /api/routines", "routines" in get("/api/routines"))
check("GET /api/model", "active" in get("/api/model"))
lang = get("/api/lang")
check("GET /api/lang", lang.get("lang") in ("it", "en"), lang)
logs = get("/api/logs?limit=20")
check("GET /api/logs (log catturati)",
      isinstance(logs.get("lines"), list) and len(logs["lines"]) >= 1,
      len(logs.get("lines", [])))
check("stats: modello STT reale in uso", bool(stats.get("models", {}).get("stt")),
      stats.get("models", {}).get("stt"))

# --- comandi testuali ---
r = post("/api/text", {"text": "che ore sono"})
check("cmd 'che ore sono'", r.get("intent") == "time"
      and any(c.isdigit() for c in r.get("assistant", "")), r)
r = post("/api/text", {"text": "elenca i file sul desktop"})
check("cmd 'elenca i file'", r.get("intent") == "list_files", r)
if HEAVY:
    r = post("/api/text", {"text": "metti il volume al 50"})
    check("cmd 'metti il volume al 50'", "50" in r.get("assistant", ""), r)
else:
    skip("cmd 'metti il volume al 50'", "test pesante (tocco il volume)")

# --- pipe conversazionale (richiede Ollama: in CI si salta) ---
if LLM_UP:
    r = post("/api/text", {"text": "quanto fa 1+1"})
    a = r.get("assistant", "")
    check("chat 'quanto fa 1+1' -> risposta col risultato",
          "2" in a and "non ho capito" not in a.lower(), f"{r.get('intent')}/{r.get('detector')}: {a}")
    r = post("/api/text", {"text": "chi ha inventato il telefono"})
    a = r.get("assistant", "")
    check("chat 'chi ha inventato il telefono'",
          len(a) > 5 and "non ho capito" not in a.lower(), f"{r.get('intent')}/{r.get('detector')}: {a}")
else:
    skip("chat 'quanto fa 1+1'")
    skip("chat 'chi ha inventato il telefono'")

# --- correzione trascrizione senza esecuzione (richiede Ollama) ---
# il test presuppone la correzione AI attiva: salvo lo stato iniziale, la
# riattivo e la ripristino alla fine (con 'qwen_stt': false il refuso resta)
_q0 = get("/api/qwen_stt").get("on", True)
if LLM_UP:
    post("/api/qwen_stt", {"on": True})
    r = post("/api/normalize", {"text": "apri spotrifyt"})
    check("normalize 'apri spotrifyt' -> spotify",
          "spotify" in (r.get("text") or "").lower(), r)
else:
    skip("normalize 'apri spotrifyt' -> spotify")

# --- multi-comando (apre davvero le pagine nel browser; solo in locale) ---
if HEAVY:
    r = post("/api/text", {"text": "apri youtube e poi github"})
    check("multi 'apri youtube e poi github'", r.get("intent") == "multi"
          and "youtube" in r.get("assistant", "").lower()
          and "github" in r.get("assistant", "").lower(), r)
else:
    skip("multi 'apri youtube e poi github'", "test pesante (apre il browser)")

# --- E2E file system: crea -> verifica -> elimina (cestino; solo in locale) ---
if HEAVY:
    r = post("/api/text", {"text": "crea una cartella chiamata ProvaTestUgo sul desktop"})
    check("cmd 'crea cartella ProvaTestUgo'", r.get("intent") == "create_folder"
          and "creat" in r.get("assistant", "").lower()  # creato/creata
          and "ProvaTestUgo" in r.get("assistant", ""), r)  # il case del nome va preservato
    desk = Path.home() / "Desktop" / "ProvaTestUgo"
    check("cartella realmente creata su disco", desk.is_dir(), desk)
    r = post("/api/text", {"text": "elimina la cartella ProvaTestUgo"})
    check("cmd 'elimina la cartella'", "eliminat" in r.get("assistant", "").lower()
          or r.get("intent") == "delete_folder", r)
    time.sleep(1.5)
    check("cartella rimossa dal desktop (nel cestino)", not desk.is_dir(), desk)
else:
    skip("E2E file system (cartelle)", "test pesante (tocco il desktop)")

# --- guardie ---
r = post("/api/text", {"text": "si"})
check("guardia 'si' senza conferme pendenti", str(r.get("intent", "")).startswith("confirm"), r)

# --- la dashboard deve aver registrato la fase chat ---
stages = {s["stage"] for s in get("/api/stats").get("stages", [])}
if LLM_UP:
    check("stats registra la fase qwen_chat", "qwen_chat" in stages, stages)
else:
    skip("stats registra la fase qwen_chat")

# --- la dashboard rispecchia lo stato della correzione AI disattivata ---
post("/api/qwen_stt", {"on": False})
_d = get("/api/stats")
check("stats pubblica qwen_stt=False", _d.get("qwen_stt") is False, _d.get("qwen_stt"))
_st = {s["stage"] for s in _d.get("stages", [])}
check("stats: fase qwen_normalize nascosta con Qwen OFF",
      "qwen_normalize" not in _st, _st)
post("/api/qwen_stt", {"on": _q0})  # ripristino dello stato iniziale

print()
okn = sum(1 for _, ok in results if ok)
print(f"RISULTATO: {okn}/{len(results)} test superati")
raise SystemExit(0 if okn == len(results) else 1)
