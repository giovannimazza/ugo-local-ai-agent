# -*- coding: utf-8 -*-
"""Test E2E delle API del server Ugo.

Richiede il server attivo (`ugo run` oppure `ugo run server`):
se non risponde su http://127.0.0.1:8123 il test esce 0 con un avviso
(usa `python tests/test_multicommand.py` per i test senza server).

Nota: i test che aprono app/siti (multi-comando) agiscono davvero sul PC.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8123"


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

# --- comandi testuali ---
r = post("/api/text", {"text": "che ore sono"})
check("cmd 'che ore sono'", r.get("intent") == "time"
      and any(c.isdigit() for c in r.get("assistant", "")), r)
r = post("/api/text", {"text": "elenca i file sul desktop"})
check("cmd 'elenca i file'", r.get("intent") == "list_files", r)
r = post("/api/text", {"text": "metti il volume al 50"})
check("cmd 'metti il volume al 50'", "50" in r.get("assistant", ""), r)

# --- pipe conversazionale ---
r = post("/api/text", {"text": "quanto fa 1+1"})
a = r.get("assistant", "")
check("chat 'quanto fa 1+1' -> risposta col risultato",
      "2" in a and "non ho capito" not in a.lower(), f"{r.get('intent')}/{r.get('detector')}: {a}")
r = post("/api/text", {"text": "chi ha inventato il telefono"})
a = r.get("assistant", "")
check("chat 'chi ha inventato il telefono'",
      len(a) > 5 and "non ho capito" not in a.lower(), f"{r.get('intent')}/{r.get('detector')}: {a}")

# --- correzione trascrizione senza esecuzione ---
r = post("/api/normalize", {"text": "apri spotrifyt"})
check("normalize 'apri spotrifyt' -> spotify",
      "spotify" in (r.get("text") or "").lower(), r)

# --- multi-comando (apre davvero le pagine nel browser) ---
r = post("/api/text", {"text": "apri youtube e poi github"})
check("multi 'apri youtube e poi github'", r.get("intent") == "multi"
      and "youtube" in r.get("assistant", "").lower()
      and "github" in r.get("assistant", "").lower(), r)

# --- E2E file system: crea -> verifica -> elimina (cestino) ---
r = post("/api/text", {"text": "crea una cartella chiamata ProvaTestUgo sul desktop"})
check("cmd 'crea cartella ProvaTestUgo'", r.get("intent") == "create_folder"
      and "creato" in r.get("assistant", "").lower(), r)
desk = Path.home() / "Desktop" / "ProvaTestUgo"
check("cartella realmente creata su disco", desk.is_dir(), desk)
r = post("/api/text", {"text": "elimina la cartella ProvaTestUgo"})
check("cmd 'elimina la cartella'", "eliminat" in r.get("assistant", "").lower()
      or r.get("intent") == "delete_folder", r)
time.sleep(1.5)
check("cartella rimossa dal desktop (nel cestino)", not desk.is_dir(), desk)

# --- guardie ---
r = post("/api/text", {"text": "si"})
check("guardia 'si' senza conferme pendenti", str(r.get("intent", "")).startswith("confirm"), r)

# --- la dashboard deve aver registrato la fase chat ---
stages = {s["stage"] for s in get("/api/stats").get("stages", [])}
check("stats registra la fase qwen_chat", "qwen_chat" in stages, stages)

print()
okn = sum(1 for _, ok in results if ok)
print(f"RISULTATO: {okn}/{len(results)} test superati")
raise SystemExit(0 if okn == len(results) else 1)
