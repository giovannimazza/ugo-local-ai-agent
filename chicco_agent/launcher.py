# -*- coding: utf-8 -*-
"""
Avvio unificato di Ugo: server + widget con un solo comando.

  python chicco_app.py          (dalla cartella del progetto)
  pythonw chicco_app.py         (senza finestra console)
  doppio click su chicco_app.py (se .py e' associato a Python)

Comportamento: se il server e' gia' attivo sulla porta lo TERMINA e riparte
pulito (l'utente ha chiesto sempre una istanza fresca); i widget desktop
duplicati vengono chiusi e ne viene avviato uno solo. La terminazione avviene
solo se il processo che occupa la porta e' un python: mai un programma estraneo.

La stessa logica e' usata da `ugo run` (con riutilizzo del server gia'
attivo) e da `ugo stop` (solo arresto mirato, senza ammazzare tutti i
pythonw di sistema).
"""
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    from . import platform_utils as pu
except ImportError:  # eseguito come script diretto
    if __package__ is None and str(Path(__file__).resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from chicco_agent import platform_utils as pu

PKG = Path(__file__).resolve().parent
PORT = 8123
URL = f"http://127.0.0.1:{PORT}"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if pu.IS_WINDOWS else 0


def _step(msg): print(f"\n==> {msg}")
def _ok(msg): print("  [OK] " + msg)
def _warn(msg): print("  [!!] " + msg)


def server_up(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(URL + "/", timeout=timeout):
            return True
    except Exception:
        return False


def _server_pid() -> int | None:
    """PID del processo in ascolto sulla porta di Ugo, se individuabile."""
    try:
        if pu.IS_WINDOWS:
            r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                               text=True, timeout=10, creationflags=_NO_WINDOW)
            for line in r.stdout.splitlines():
                p = line.split()
                if len(p) >= 5 and p[1].endswith(f":{PORT}") and p[3] == "LISTENING":
                    return int(p[4])
        else:
            r = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True,
                               text=True, timeout=10)
            out = r.stdout.strip().split()
            if out and out[0].isdigit():
                return int(out[0])
    except Exception:
        pass
    return None


def _pid_name(pid: int) -> str:
    """Nome immagine del processo (per non uccidere mai un programma estraneo)."""
    try:
        if pu.IS_WINDOWS:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=10,
                               creationflags=_NO_WINDOW)
            line = (r.stdout or "").strip().splitlines()
            if line and line[0].startswith('"'):
                return line[0].split('","')[0].strip('"').lower()
        else:
            r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                               capture_output=True, text=True, timeout=10)
            return (r.stdout or "").strip().split()[-1].lower() if r.stdout.strip() else ""
    except Exception:
        pass
    return ""


def _kill_pid(pid: int) -> bool:
    try:
        if pu.IS_WINDOWS:
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                               text=True, timeout=15, creationflags=_NO_WINDOW)
            return r.returncode == 0
        subprocess.run(["kill", str(pid)], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _widget_pids() -> list:
    """PID dei widget Ugo attivi (solo quelli, non tutti i python)."""
    try:
        if pu.IS_WINDOWS:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                  "Where-Object { $_.CommandLine -match 'chicco_agent' "
                  "-and $_.CommandLine -match 'widget.py' } | "
                  "Select-Object -ExpandProperty ProcessId")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=20,
                               creationflags=_NO_WINDOW)
            return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        r = subprocess.run(["pgrep", "-f", "chicco_agent/widget.py"],
                           capture_output=True, text=True, timeout=10)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _is_python(name: str) -> bool:
    n = (name or "").lower()
    return n.startswith(("python", "py ")) or "python" in n


def stop_all(verbose: bool = True) -> None:
    """Arresto mirato: widget Ugo + processo in ascolto sulla porta."""
    for pid in _widget_pids():
        if _kill_pid(pid) and verbose:
            _ok(f"widget fermato (PID {pid})")
    pid = _server_pid()
    if pid:
        name = _pid_name(pid)
        if not _is_python(name):
            if verbose:
                _warn(f"la porta {PORT} e' occupata da {name or 'processo sconosciuto'}"
                      f" (PID {pid}): NON lo tocco")
            return
        if _kill_pid(pid) and verbose:
            _ok(f"server fermato (PID {pid})")
        for _ in range(20):  # aspetto che la porta si liberi davvero
            if _server_pid() is None:
                break
            time.sleep(0.5)


def start_all(reuse: bool = False, skip_widget: bool = False) -> int:
    """Avvio completo: check aggiornamenti -> pulizia istanze -> server -> widget.
    reuse=True: se il server e' gia' attivo e sano, non lo riavvia."""
    _step("Pulizia istanze precedenti")
    # controllo versione su GitHub: se c'e' una versione piu' recente aggiorna
    # e rilancia se stesso (interattivo da terminale, silenzioso al doppio click)
    try:
        from . import update
    except ImportError:
        from chicco_agent import update
    update.auto_check_and_restart()

    widgets = _widget_pids()
    for pid in widgets:
        if _kill_pid(pid):
            _ok(f"widget duplicato chiuso (PID {pid})")
    if not widgets:
        _ok("nessun widget duplicato")

    pid = _server_pid()
    if pid:
        name = _pid_name(pid)
        if not _is_python(name):
            _warn(f"la porta {PORT} e' occupata da {name or 'sconosciuto'} (PID {pid}): "
                  "non posso avviare il server")
            return 2
        if reuse and server_up():
            _ok(f"server gia' attivo e sano (PID {pid}): lo riuso")
        else:
            _ok(f"istanza precedente trovata (PID {pid}): kill automatico")
            _kill_pid(pid)
            for _ in range(20):
                if _server_pid() is None:
                    break
                time.sleep(0.5)
    else:
        _ok("porta libera")

    _step(f"Avvio server su {URL}")
    if not server_up():
        print("  (primo avvio: carico Laya + Vosk, puo' volerci un minuto...)")
        pu.popen_hidden([sys.executable, str(PKG / "server.py")],
                        cwd=str(PKG.parent),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for i in range(90):
            time.sleep(1)
            if server_up():
                break
            if i in (15, 30, 45, 60):
                print("  ancora in avvio...")
    if not server_up():
        _warn("il server non e' partito: lancialo a mano per vedere l'errore:")
        print(f"    python {PKG / 'server.py'}")
        return 1
    _ok("server attivo")

    if not skip_widget:
        _step("Avvio widget desktop")
        exe = sys.executable
        if pu.IS_WINDOWS:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            exe = str(pythonw) if pythonw.exists() else sys.executable
        pu.popen_hidden([str(exe), str(PKG / "widget.py")], cwd=str(PKG.parent))
        _ok("widget avviato (guarda nell'angolo dello schermo)")

    print("\nUgo e' pronto. Parla col microfono o scrivi nella pillola.")
    print("Chiudi il widget: click destro sul cerchio.  Stop server:  ugo stop")
    return 0
