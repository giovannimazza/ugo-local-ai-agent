# -*- coding: utf-8 -*-
"""Ugo — installazione automatica completa (Windows).

Un solo comando, nessun prerequisito oltre a Windows 10/11:

    powershell -c "irm https://raw.githubusercontent.com/giovannimazza/ugo-local-ai-agent/main/boot/install.py | iex"

Cosa fa:
  1. Python: usa quello presente se >= 3.10, altrimenti installa via winget
     (o scarica l'installer ufficiale) con l'opzione PATH
  2. scarica il sorgente Ugo (zip GitHub, nessun prerequisito git)
  3. crea un venv dedicato e installa il pacchetto con le dipendenze
  4. lancia `ugo setup` dentro il venv (Ollama, modelli Qwen, Whisper, Vosk,
     Piper) e infine avvia server + widget

Gli aggiornamenti restano automatici (`ugo update`); questo script e' solo
il primo avvio.
"""
import ctypes
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "https://github.com/giovannimazza/ugo-local-ai-agent"
BRANCH = "main"
DEST = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Ugo"
VENV = DEST / "venv"
SRC = DEST / "ugo-local-ai-agent"
PY_MIN = (3, 10)


def run(cmd, **kw):
    print("  " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, **kw)


def step(msg):
    print(f"\n==> {msg}")


def ok(msg):
    print(f"    OK: {msg}")


def die(msg):
    print(f"\nERRORE: {msg}")
    try:
        input("Invio per chiudere...")
    except EOFError:
        pass
    sys.exit(1)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------- Python ----
def py_exe() -> str:
    """Il miglior Python >= 3.10 gia' presente, altrimenti lo installa.
    SI PREFERISCE IL 3.12: su 3.14 pycaw/comtypes hanno un crash nativo noto
    (_ctypes 0xc0000005) che puo' colpire il server; il worker audio lo isola,
    ma il 3.12 resta la scelta stabile."""
    def _version(exe: str) -> tuple | None:
        try:
            v = subprocess.run([exe, "-c", "import sys;print(sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
            return eval(v) if v.startswith("(") else None
        except Exception:
            return None

    found = []
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("Program Files", "")):
        if base:
            found += [str(p) for p in Path(base).glob("Python3*/python.exe")]
            found += [str(p) for p in Path(base).glob("Programs/Python/Python3*/python.exe")]
    for c in ("python", "python3", "py"):
        f = shutil_which(c)
        if f:
            found.insert(0, f if f.endswith(".exe") else c)

    def _pref(v: tuple) -> int:
        if v[:2] == (3, 12):
            return 0                      # scelta consigliata
        if (3, 10) <= v[:2] <= (3, 11):
            return 1
        return 2                          # 3.13+: crash COM noto, mitigato dal worker

    seen, best = set(), None
    for exe in found:
        v = _version(exe)
        if not v or v < PY_MIN:
            continue
        key = exe.lower()
        if key in seen:
            continue
        seen.add(key)
        if best is None or _pref(v) < _pref(best[1]):
            best = (exe, v)
    if best:
        ok(f"Python {best[1][0]}.{best[1][1]} trovato: {best[0]}")
        return best[0]
    return install_python()


def shutil_which(name):
    from shutil import which
    return which(name)


def install_python() -> str:
    step("Installo Python 3.12 (nessun Python >= 3.10 presente)")
    exe = None
    if shutil_which("winget"):
        run(["winget", "install", "--id", "Python.Python.3.12", "--silent",
             "--accept-package-agreements", "--accept-source-agreements"])
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("Program Files", "")):
            if base:
                hits = sorted(Path(base).glob("Python3.12*/python.exe")) + \
                       sorted(Path(base).glob("Programs/Python/Python312/python.exe"))
                if hits:
                    exe = str(hits[-1])
                    break
    if not exe:  # niente winget: installer ufficiale per l'utente corrente
        step("winget non trovato: scarico l'installer ufficiale di Python")
        url = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
        inst = DEST / "python-installer.exe"
        DEST.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, inst)
        run([str(inst), "/quiet", "InstallAllUsers=0", "PrependPath=1",
             "Include_test=0"], check=True)
        inst.unlink(missing_ok=True)
        exe = str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python"
                  / "Python312" / "python.exe")
    if not exe or not Path(exe).exists():
        die("installazione Python non riuscita; installalo da python.org e rilancia")
    ok(f"Python installato: {exe}")
    return exe


# ------------------------------------------------------------------ repo ----
def get_source() -> Path:
    step(f"Scarico Ugo da GitHub ({BRANCH})")
    zip_path = DEST / "ugo.zip"
    DEST.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(f"{REPO}/archive/refs/heads/{BRANCH}.zip", zip_path)
    if SRC.exists():
        import shutil
        shutil.rmtree(SRC, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(DEST)
    zip_path.unlink(missing_ok=True)
    inner = next(DEST.glob("ugo-local-ai-agent-*"))
    inner.rename(SRC)
    ok(f"sorgente in {SRC}")
    return SRC


# ------------------------------------------------------------------ venv ----
def make_venv(py: str) -> str:
    step("Creo l'ambiente Python dedicato (venv)")
    run([py, "-m", "venv", str(VENV)], check=True)
    ok(f"venv in {VENV}")
    return str(VENV / "Scripts" / "python.exe")


def pip_install(vp: str) -> None:
    step("Installo ugo-agent e le dipendenze (puo' volere qualche minuto)")
    run([vp, "-m", "pip", "install", "--upgrade", "pip"], check=True)
    r = run([vp, "-m", "pip", "install", "-e", str(SRC)])
    if r.returncode != 0:
        die("installazione pacchetto fallita: leggi gli errori qui sopra")


# ----------------------------------------------------------------- main -----
def main():
    print("=" * 62)
    print(" Ugo - assistente vocale locale - installazione automatica")
    print("=" * 62)
    if is_admin():
        print("\nNOTA: stai usando un terminale ADMIN: non e' necessario e il")
        print("microfono/la portable app potrebbero comportarsi peggio.")
        print("Chiudi e rilancia da un terminale normale se puoi.")
    py = py_exe()
    if SRC.exists() and (VENV / "Scripts" / "python.exe").exists():
        step("Installazione gia' presente: aggiorno il codice")
        run([str(VENV / "Scripts" / "python.exe"), "-m", "pip", "install", "-e", str(SRC), "--quiet"])
    else:
        get_source()
        vp = make_venv(py)
        pip_install(vp)
    ugo = str(VENV / "Scripts" / "ugo.exe")
    if not Path(ugo).exists():
        die("ugo.exe non creato: controlla gli errori sopra")
    step("Setup componenti (Ollama, modelli, Whisper, Vosk, Piper)")
    run([ugo, "setup"])
    step("Avvio Ugo (server + widget)")
    run([ugo, "run"])
    print("\n" + "=" * 62)
    print(" Ugo e' installato!")
    print(f"   cartella:   {DEST}")
    print(f"   avvio:      doppio click su {ugo.replace('.exe', '_app.exe') if Path(ugo.replace('.exe', '_app.exe')).exists() else ugo}")
    print("   aggiornamenti automatici:  ugo update")
    print("=" * 62)
    try:
        input("\nInvio per chiudere questa finestra...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
