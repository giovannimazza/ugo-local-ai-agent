# -*- coding: utf-8 -*-
"""
Worker audio in subprocess: ogni operazione COM/pycaw vive in un processo
SEPARATO dal server.

Motivo: su Python 3.14 le chiamate pycaw/comtypes possono causare un crash
nativo di _ctypes.pyd (0xc0000005, heap COM) che uccide l'INTERO server.
Con il worker il crash al peggio degrada una singola richiesta di volume
(risposta di errore) invece di buttare giu' STT, LLM e TTS.

Protocollo: UNA riga JSON su stdin -> UNA riga JSON su stdout, in loop.
Il worker e' RESIDENTE: il server lo avvia alla prima richiesta di volume e
lo riutilizza per tutte le successive (prima: spawn per richiesta, ~300 ms
di overhead a ogni comando). Se il worker crasha (proprio il crash COM che
deve isolare) il server lo rileva e lo riavvia, ripetendo UNA volta la
richiesta fallita: da fuori la richiesta al peggio perde ~1 s, non il server.

Richieste (una per riga):
  {"op": "ping"}                               -> {"ok": true, "pid": 1234}
  {"op": "master_get"}                        -> {"ok": true, "volume": 50, "mute": false}
  {"op": "master_set", "volume": 40}          -> {"ok": true, "volume": 40}
  {"op": "master_mute", "mute": true}         -> {"ok": true, "mute": true}
  {"op": "sessions"}                          -> {"ok": true, "sessions": [{"name": "discord", "volume": 70}]}
  {"op": "app_get", "app": "discord"}         -> {"ok": true, "volume": 70}
  {"op": "app_set", "app": "discord", "volume": 30} -> {"ok": true, "volume": 30}
  {"op": "quit"}                              -> {"ok": true}  (poi il worker esce)

Il server lo usa tramite le funzioni comode in fondo (worker_call,
worker_master_get, ...): fuori da Windows o senza pycaw ritornano None e il
chiamante ricade sul comportamento preesistente.
"""
import atexit
import json
import os
import sys
import threading
import time


def _endpoint_volume():
    """Puntatore IAudioEndpointVolume (stessa compatibilita' di server.py)."""
    import comtypes
    from ctypes import cast, POINTER
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    comtypes.CoInitialize()
    dev = AudioUtilities.GetSpeakers()
    if getattr(dev, "EndpointVolume", None):
        return cast(dev.EndpointVolume, POINTER(IAudioEndpointVolume))
    iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def op_master_get():
    vol = _endpoint_volume()
    return {"volume": int(round(vol.GetMasterVolumeLevelScalar() * 100)),
            "mute": bool(vol.GetMute())}


def op_master_set(volume: int):
    v = max(0, min(100, int(volume)))
    vol = _endpoint_volume()
    vol.SetMasterVolumeLevelScalar(v / 100.0, None)
    return {"volume": int(round(vol.GetMasterVolumeLevelScalar() * 100))}


def op_master_mute(mute: bool):
    vol = _endpoint_volume()
    vol.SetMute(int(bool(mute)), None)
    return {"mute": bool(vol.GetMute())}


def op_sessions():
    from pycaw.pycaw import AudioUtilities
    import comtypes
    comtypes.CoInitialize()
    out = []
    for s in AudioUtilities.GetAllSessions():
        try:
            if s.Process is None or s.SimpleAudioVolume is None:
                continue
            name = (s.Process.name() or "").lower().removesuffix(".exe")
            if name:
                out.append({"name": name,
                            "volume": int(round(s.SimpleAudioVolume.GetMasterVolume() * 100))})
        except Exception:
            continue
    return {"sessions": out}


def _app_volume(app: str):
    """SimpleAudioVolume della sessione che combacia col nome, oppure None."""
    from pycaw.pycaw import AudioUtilities
    import comtypes
    comtypes.CoInitialize()
    n = (app or "").lower().strip()
    for s in AudioUtilities.GetAllSessions():
        try:
            if s.Process is None or s.SimpleAudioVolume is None:
                continue
            name = (s.Process.name() or "").lower().removesuffix(".exe")
            if name and (n == name or n in name or name in n):
                return s.SimpleAudioVolume
        except Exception:
            continue
    return None


def op_app_get(app: str):
    vol = _app_volume(app)
    if vol is None:
        return {"error": "sessione non trovata"}
    return {"volume": int(round(vol.GetMasterVolume() * 100))}


def op_app_set(app: str, volume: int):
    vol = _app_volume(app)
    if vol is None:
        return {"error": "sessione non trovata"}
    v = max(0.0, min(1.0, int(volume) / 100.0))
    vol.SetMasterVolume(v, None)
    return {"volume": int(round(vol.GetMasterVolume() * 100))}


def _handle(req: dict) -> dict:
    op = req.get("op")
    if op == "ping":
        return {"pid": os.getpid()}  # compilato in main() col PID vero del worker
    if op == "master_get":
        return op_master_get()
    if op == "master_set":
        return op_master_set(int(req["volume"]))
    if op == "master_mute":
        return op_master_mute(bool(req.get("mute")))
    if op == "sessions":
        return op_sessions()
    if op == "app_get":
        return op_app_get(str(req.get("app") or ""))
    if op == "app_set":
        return op_app_set(str(req.get("app") or ""), int(req["volume"]))
    return {"error": f"op sconosciuta: {op!r}"}


def main() -> int:
    """Worker RESIDENTE: legge una richiesta per riga da stdin, scrive una
    risposta per riga su stdout, finche' lo stdin non si chiude o arriva
    {"op": "quit"}. Il PID va nella risposta a ping (per i test)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = {}
        try:
            req = json.loads(line)
            out = _handle(req)
            out.setdefault("ok", True)
        except Exception as exc:
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        except Exception:
            break  # il server e' morto: niente a cui rispondere
        if isinstance(req, dict) and req.get("op") == "quit":
            break
    return 0


# ---------------------------------------------------------------------------
# Lato server: helper per chiamare il worker RESIDENTE (questo modulo e'
# importabile anche dal server, la parte subprocess sta qui per condividere
# la logica). Primo uso: spawn; usi successivi: riga su stdin/risposta.
# ---------------------------------------------------------------------------
_WORKER_TIMEOUT = 10.0
_proc = None                    # handle del worker residente
_proc_lock = threading.Lock()   # le richieste si serializzano: un solo pipe


def _stop_worker() -> None:
    """Chiude il worker (crash sospetto, hang o uscita dal server)."""
    global _proc
    p, _proc = _proc, None
    if p is None:
        return
    try:
        p.kill()
    except Exception:
        pass


def _call_locked(req: dict, retries: int = 1) -> dict | None:
    """Chiamata con lock GIA' preso: spawn se necessario, write/read, e in
    caso di crash/hang kill+respawn con retry (max 'retries' volte)."""
    global _proc
    import subprocess
    from pathlib import Path

    if _proc is not None and _proc.poll() is not None:
        _proc = None              # e' crashato: era il suo compito
    if _proc is None:
        try:
            _proc = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            atexit.register(_stop_worker)
        except Exception:
            _proc = None
            return None
    try:
        _proc.stdin.write(json.dumps(req).encode() + b"\n")
        _proc.stdin.flush()
    except Exception:
        _stop_worker()
        return _call_locked(req, retries - 1) if retries > 0 else None
    # lettura CON timeout: un worker appeso viene killato, non blocca
    # la pipeline vocale (prima: subprocess.run con timeout, stesso patto)
    box: dict = {}

    def _read():
        try:
            box["line"] = _proc.stdout.readline()
        except Exception:
            box["line"] = b""

    th = threading.Thread(target=_read, daemon=True)
    th.start()
    th.join(_WORKER_TIMEOUT)
    if th.is_alive():
        _stop_worker()            # readline sbloccata dalla kill
        return _call_locked(req, retries - 1) if retries > 0 else None
    line = box.get("line") or b""
    if not line.strip():
        _stop_worker()            # stdout chiuso: il worker e' morto
        return _call_locked(req, retries - 1) if retries > 0 else None
    try:
        return json.loads(line.decode(errors="replace"))
    except Exception:
        _stop_worker()
        return _call_locked(req, retries - 1) if retries > 0 else None


def worker_call(req: dict) -> dict | None:
    """Esegue una richiesta nel worker subprocess RESIDENTE. Ritorna la
    risposta con 'ok', o None se fuori da Windows / pycaw assente / worker
    non riavviabile. Il chiamante deve gestire None ricadendo sul percorso
    preesistente. Su crash o hang del worker: kill, respawn e UNO retry."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import pycaw  # noqa: F401  disponibilita': senza, tutto il ramo e' OFF
    except Exception:
        return None
    with _proc_lock:
        return _call_locked(req)


def worker_master_get() -> dict | None:
    return worker_call({"op": "master_get"})


def worker_master_set(volume: int) -> dict | None:
    return worker_call({"op": "master_set", "volume": int(volume)})


def worker_master_mute(mute: bool) -> dict | None:
    return worker_call({"op": "master_mute", "mute": bool(mute)})


def worker_sessions() -> list | None:
    r = worker_call({"op": "sessions"})
    return r.get("sessions") if r and r.get("ok") else None


def worker_app_get(app: str) -> dict | None:
    r = worker_call({"op": "app_get", "app": app})
    return r if r and r.get("ok") else None


def worker_app_set(app: str, volume: int) -> dict | None:
    r = worker_call({"op": "app_set", "app": app, "volume": int(volume)})
    return r if r and r.get("ok") else None


if __name__ == "__main__":
    sys.exit(main())
