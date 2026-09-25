# -*- coding: utf-8 -*-
"""
Worker audio in subprocess: ogni operazione COM/pycaw vive in un processo
SEPARATO dal server.

Motivo: su Python 3.14 le chiamate pycaw/comtypes possono causare un crash
nativo di _ctypes.pyd (0xc0000005, heap COM) che uccide l'INTERO server.
Con il worker il crash al peggio degrada una singola richiesta di volume
(risposta di errore) invece di buttare giu' STT, LLM e TTS.

Protocollo: richiesta JSON su stdin, risposta JSON su stdout, una per
processo (spawn per richiesta: le chiamate sono brevi e rare).

Richieste:
  {"op": "master_get"}                        -> {"ok": true, "volume": 50, "mute": false}
  {"op": "master_set", "volume": 40}          -> {"ok": true, "volume": 40}
  {"op": "master_mute", "mute": true}         -> {"ok": true, "mute": true}
  {"op": "sessions"}                          -> {"ok": true, "sessions": [{"name": "discord", "volume": 70}]}
  {"op": "app_get", "app": "discord"}         -> {"ok": true, "volume": 70}
  {"op": "app_set", "app": "discord", "volume": 30} -> {"ok": true, "volume": 30}

Il server lo usa tramite le funzioni comode in fondo (worker_call,
worker_master_get, ...): fuori da Windows o senza pycaw ritornano None e il
chiamante ricade sul comportamento preesistente.
"""
import json
import sys


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
    """Una richiesta per processo: legge JSON da stdin, scrive JSON su stdout."""
    try:
        req = json.loads(sys.stdin.read() or "{}")
        out = _handle(req)
        out.setdefault("ok", True)
    except Exception as exc:
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    return 0 if out.get("ok") else 1


# ---------------------------------------------------------------------------
# Lato server: helper per chiamare il worker (questo modulo e' importabile
# anche dal server, la parte subprocess sta qui per condividere la logica).
# ---------------------------------------------------------------------------
_WORKER_TIMEOUT = 10.0


def worker_call(req: dict) -> dict | None:
    """Esegue una richiesta nel worker subprocess. Ritorna la risposta con
    'ok', o None se fuori da Windows / pycaw assente / worker non disponibile.
    Il chiamante deve gestire None ricadendo sul percorso preesistente."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import pycaw  # noqa: F401  disponibilita': senza, tutto il ramo e' OFF
    except Exception:
        return None
    import subprocess
    from pathlib import Path
    creation = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    try:
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            input=json.dumps(req).encode(), capture_output=True,
            timeout=_WORKER_TIMEOUT, creationflags=creation)
        return json.loads(r.stdout.decode(errors="replace"))
    except Exception:
        return None


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
