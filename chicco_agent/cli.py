# -*- coding: utf-8 -*-
"""
Interfaccia a riga di comando di Chicco.

  chicco setup   -> installa/verifica TUTTO (Ollama, modelli, dipendenze)
  chicco run     -> avvia server + widget (installa cio' che manca prima)
  chicco doctor  -> diagnostica: cosa e' installato, cosa manca

Uso tipico su una macchina nuova:
  pip install git+https://github.com/giovannimazza/chicco-local-ai-agent.git
  chicco run
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8123
DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "chicco"
WHISPER_GGUF = Path.home() / ".cache" / "whisper" / "whisper-large-v3-turbo-Q8_0.gguf"
WHISPER_URL = ("https://huggingface.co/handy-computer/whisper-large-v3-turbo-gguf/"
               "resolve/main/whisper-large-v3-turbo-Q8_0.gguf")
VOSK_DIR = Path.home() / ".cache" / "vosk" / "vosk-model-small-it-0.22"
VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip"
OLLAMA_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
PKG = Path(__file__).resolve().parent


def _step(msg):
    print(f"\n==> {msg}")


def _ok(msg):
    print("  [OK] " + msg)


def _warn(msg):
    print("  [!!] " + msg)


def _fail(msg):
    print("  [XX] " + msg)


# ---------------------------------------------------------------------------
# controlli
# ---------------------------------------------------------------------------
def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2):
            return True
    except Exception:
        return False


def ollama_ok() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=3):
            return True
    except Exception:
        return False


def qwen_ok() -> bool:
    if not OLLAMA_EXE.exists():
        return False
    try:
        out = subprocess.run([str(OLLAMA_EXE), "list"], capture_output=True,
                             text=True, timeout=15).stdout
        return "qwen2.5" in out
    except Exception:
        return False


def py_import(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# azioni di installazione
# ---------------------------------------------------------------------------
def install_pip_deps() -> None:
    _step("Dipendenze Python")
    missing = []
    for mod, pkg in [("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                     ("laya", "laya"), ("pyttsx3", "pyttsx3"),
                     ("vosk", "vosk"), ("soundcard", "soundcard"),
                     ("numpy", "numpy"), ("PIL", "pillow"),
                     ("send2trash", "send2trash"), ("pycaw", "pycaw"),
                     ("comtypes", "comtypes"), ("transcribe_cpp", "transcribe-cpp")]:
        if not py_import(mod):
            missing.append(pkg)
    if missing:
        print("  installo:", ", ".join(missing))
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
                               "--disable-pip-version-check", *missing])
    _ok("dipendenze Python pronte")


def install_ollama() -> None:
    _step("Ollama (LLM locale)")
    if ollama_ok():
        _ok("server Ollama attivo")
        return
    if not OLLAMA_EXE.exists():
        print("  installo via winget (~1 min)...")
        r = subprocess.run(["winget", "install", "Ollama.Ollama", "-e", "--silent",
                            "--accept-source-agreements", "--accept-package-agreements"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _fail("winget non e' riuscito: installa Ollama da https://ollama.com")
            return
    # avvia il server se non risponde
    if not ollama_ok():
        subprocess.Popen([str(OLLAMA_EXE), "serve"],
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        for _ in range(30):
            time.sleep(1)
            if ollama_ok():
                break
    _ok("Ollama pronto") if ollama_ok() else _fail("Ollama non risponde")


def install_qwen() -> None:
    _step("Modello Qwen2.5 0.5B (traduzione comandi -> JSON)")
    if qwen_ok():
        _ok("qwen2.5:0.5b gia' presente")
        return
    if not ollama_ok():
        _fail("serve Ollama attivo")
        return
    print("  scarico ~400 MB...")
    subprocess.run([str(OLLAMA_EXE), "pull", "qwen2.5:0.5b"])
    _ok("qwen2.5:0.5b installato") if qwen_ok() else _fail("pull fallito")


def install_whisper() -> None:
    _step("Whisper large-v3-turbo Q8_0 (~874 MB, STT su GPU)")
    if WHISPER_GGUF.exists():
        _ok(f"gia' presente: {WHISPER_GGUF}")
        return
    WHISPER_GGUF.parent.mkdir(parents=True, exist_ok=True)
    tmp = WHISPER_GGUF.with_suffix(".part")
    print("  download da Hugging Face...")
    try:
        urllib.request.urlretrieve(WHISPER_URL, tmp)
        tmp.rename(WHISPER_GGUF)
        _ok("Whisper installato")
    except Exception as exc:
        _warn(f"download fallito ({exc}): si usera' Vosk (piu' semplice)")


def install_vosk() -> None:
    _step("Modello Vosk italiano (fallback STT, ~48 MB)")
    if VOSK_DIR.is_dir():
        _ok("gia' presente")
        return
    import io
    import zipfile
    VOSK_DIR.parent.mkdir(parents=True, exist_ok=True)
    z = VOSK_DIR.parent / "vosk-it.zip"
    print("  download...")
    urllib.request.urlretrieve(VOSK_URL, z)
    with zipfile.ZipFile(z) as f:
        f.extractall(VOSK_DIR.parent)
    z.unlink()
    _ok("Vosk installato") if VOSK_DIR.is_dir() else _fail("estrazione fallita")


# ---------------------------------------------------------------------------
# comandi
# ---------------------------------------------------------------------------
def cmd_setup() -> int:
    print("Chicco setup — installo tutto il necessario\n")
    install_pip_deps()
    install_ollama()
    install_qwen()
    install_whisper()
    install_vosk()
    print("\nSetup completato. Avvia con:  chicco run")
    return 0


def cmd_doctor() -> int:
    print("Chicco doctor — stato dell'installazione\n")
    checks = [
        ("Dipendenze Python (fastapi)", py_import("fastapi") and py_import("uvicorn")),
        ("Laya (intent)", py_import("laya")),
        ("Whisper GGUF", WHISPER_GGUF.exists()),
        ("Vosk it", VOSK_DIR.is_dir()),
        ("Ollama installato", OLLAMA_EXE.exists()),
        ("Ollama attivo", ollama_ok()),
        ("Qwen2.5 0.5B", qwen_ok()),
        ("Server Chicco attivo", server_up()),
    ]
    for name, okflag in checks:
        (_ok if okflag else _warn)(f"{name}")
    missing = [n for n, o in checks if not o]
    print()
    if missing:
        print("Mancanze: " + ", ".join(missing) + "\nRimedio con:  chicco setup")
    else:
        print("Tutto pronto! Avvia con:  chicco run")
    return 0


def cmd_run(only: str | None = None) -> int:
    # setup leggero: solo cio' che manca davvero
    install_pip_deps()
    if not (ollama_ok() and qwen_ok()):
        install_ollama()
        install_qwen()
    if not WHISPER_GGUF.exists() and os.environ.get("WHISPER", "1") == "1":
        install_whisper()
    if not VOSK_DIR.is_dir():
        install_vosk()

    _step("Avvio server su http://127.0.0.1:8123")
    detached = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    if not server_up():
        print("  (primo avvio: carico Laya + Vosk, puo' volerci un minuto...)")
        subprocess.Popen([sys.executable, str(PKG / "server.py")],
                         cwd=str(PKG.parent), creationflags=detached,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            time.sleep(1)
            if server_up():
                break
    if not server_up():
        _fail("il server non e' partito: lancialo a mano per vedere l'errore:")
        print(f"    python {PKG / 'server.py'}")
        return 1
    _ok("server attivo")

    if only != "server":
        _step("Avvio widget desktop")
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = pythonw if pythonw.exists() else sys.executable
        subprocess.Popen([str(exe), str(PKG / "widget.py")],
                         cwd=str(PKG.parent), creationflags=detached)
        _ok("widget avviato (guarda in basso a destra)")

    print("\nChicco e' pronto. Parla col microfono o scrivi nella pillola.")
    print("Chiudi il widget: click destro sul cerchio.  Stop server:  chicco stop")
    return 0


def cmd_stop() -> int:
    _step("Arresto")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "pythonw.exe"],
                       capture_output=True)
        _ok("widget fermato")
    except Exception:
        pass
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-NetTCPConnection -LocalPort 8123 -State Listen "
                              "-ErrorAction SilentlyContinue | Select -Exp OwningProcess"],
                             capture_output=True, text=True).stdout.split()
        for pid in {p.strip() for p in out if p.strip().isdigit()}:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        _ok("server fermato")
    except Exception:
        pass
    return 0


def main() -> int:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    if cmd in ("setup", "install"):
        return cmd_setup()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "stop":
        return cmd_stop()
    if cmd in ("run", "start"):
        only = sys.argv[2].lower() if len(sys.argv) > 2 else None
        return cmd_run(only)
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    print(f"comando sconosciuto: {cmd}\n" + __doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
