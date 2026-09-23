# -*- coding: utf-8 -*-
"""
Interfaccia a riga di comando di Ugo.

  ugo setup   -> installa/verifica TUTTO (Ollama, modelli, dipendenze)
  ugo run     -> avvia server + widget (installa cio' che manca prima)
  ugo doctor  -> diagnostica: cosa e' installato, cosa manca
  ugo update  -> aggiorna all'ultima versione (canale dev o stable)
  ugo channel -> canale di aggiornamento: dev (main) o stable (release tag)

Uso tipico su una macchina nuova:
  pip install git+https://github.com/giovannimazza/ugo-local-ai-agent.git
  ugo run
"""
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path

PORT = 8123
try:
    from . import platform_utils as pu
except ImportError:  # eseguito come script diretto
    if __package__ is None and str(Path(__file__).resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ugo_agent import platform_utils as pu
from ugo_agent import piper_tts

DATA = pu.data_dir()
FW_DIR = Path.home() / ".cache" / "whisper" / "faster-whisper-large-v3-turbo"
FW_REPO = "deepdml/faster-whisper-large-v3-turbo-ct2"
FW_BASE = ("https://huggingface.co/deepdml/faster-whisper-large-v3-turbo-ct2/"
           "resolve/main/")
FW_FILES = ("config.json", "model.bin", "preprocessor_config.json",
            "tokenizer.json", "vocabulary.json")
VOSK_DIR = Path.home() / ".cache" / "vosk" / "vosk-model-small-it-0.22"
VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip"
PKG = Path(__file__).resolve().parent


def _find_ollama() -> Path:
    """Percorso del binario ollama: posizione standard su Windows, poi PATH."""
    if pu.IS_WINDOWS:
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if exe.exists():
            return exe
    found = shutil.which("ollama")
    return Path(found) if found else Path("ollama")


OLLAMA_EXE = _find_ollama()  # usato anche da doctor/qwen_ok per .exists()


def _ollama_exe() -> str:
    return str(OLLAMA_EXE)


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


def qwen_tag_ok(tag: str) -> bool:
    if not OLLAMA_EXE.exists():
        return False
    try:
        out = subprocess.run([str(OLLAMA_EXE), "list"], capture_output=True,
                             text=True, timeout=15).stdout
        return tag in out
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
    common = [("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
              ("laya", "laya"), ("pyttsx3", "pyttsx3"),
              ("vosk", "vosk"), ("soundcard", "soundcard"),
              ("numpy", "numpy"), ("PIL", "pillow"),
              ("send2trash", "send2trash"),
              ("faster_whisper", "faster-whisper")]
    if pu.IS_WINDOWS:
        common += [("pycaw", "pycaw"), ("comtypes", "comtypes")]
    missing = []
    for mod, pkg in common:
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
    if not shutil.which("ollama"):
        if pu.IS_WINDOWS:
            print("  installo via winget (~1 min)...")
            r = subprocess.run(["winget", "install", "Ollama.Ollama", "-e", "--silent",
                                "--accept-source-agreements", "--accept-package-agreements"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                _fail("winget non e' riuscito: installa Ollama da https://ollama.com")
                return
        elif pu.IS_MAC:
            if not shutil.which("brew"):
                _fail("Homebrew mancante: installalo da https://brew.sh poi rilancia")
                return
            print("  installo via brew (~1 min)...")
            r = subprocess.run(["brew", "install", "ollama"], capture_output=True, text=True)
            if r.returncode != 0:
                _fail("brew non e' riuscito: installa Ollama da https://ollama.com")
                return
        else:
            print("  installo via script ufficiale (~1 min)...")
            r = subprocess.run("curl -fsSL https://ollama.com/install.sh | sh",
                               shell=True, capture_output=True, text=True)
            if r.returncode != 0:
                _fail("installer non riuscito: installa Ollama da https://ollama.com")
                return
    # avvia il server se non risponde
    if not ollama_ok():
        pu.popen_hidden([_ollama_exe(), "serve"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(1)
            if ollama_ok():
                break
    _ok("Ollama pronto") if ollama_ok() else _fail("Ollama non risponde")


def install_qwen() -> None:
    _step("Modelli Qwen2.5 (correzione trascrizione + comandi -> JSON)")
    if not ollama_ok():
        _fail("serve Ollama attivo")
        return
    # 1.5b e' il default del server, 0.5b resta il fallback di sicurezza
    for tag, mb in (("qwen2.5:1.5b", "~1000"), ("qwen2.5:0.5b", "~400")):
        if qwen_tag_ok(tag):
            _ok(f"{tag} gia' presente")
            continue
        print(f"  scarico {tag} ({mb} MB)...")
        subprocess.run([_ollama_exe(), "pull", tag])
        _ok(f"{tag} installato") if qwen_tag_ok(tag) else _fail(f"pull {tag} fallito")


def _fw_file_present() -> bool:
    return FW_DIR.is_dir() and (FW_DIR / "model.bin").is_file()


def install_whisper() -> None:
    _step("Whisper large-v3-turbo CTranslate2 (~1.6 GB, tutti gli OS)")
    if _fw_file_present():
        _ok(f"gia' presente: {FW_DIR}")
        return
    FW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  download da Hugging Face ({FW_REPO})...")
    try:
        for name in FW_FILES:
            print(f"    {name}...")
            dest = FW_DIR / name
            urllib.request.urlretrieve(FW_BASE + name, str(dest) + ".part")
            Path(str(dest) + ".part").rename(dest)
        _ok("Whisper installato")
    except Exception as exc:
        _warn(f"download fallito ({exc}): si usera' Vosk (piu' semplice)")


def ensure_path() -> None:
    """Rende il comando 'ugo' richiamabile da qualsiasi terminale:
    su Windows aggiunge la dir Scripts di pip al PATH utente (con broadcast
    WM_SETTINGCHANGE, niente riavvio), su macOS/Linux crea uno shim in
    ~/.local/bin."""
    _step("Comando 'ugo' nel PATH")
    # pip puo' mettere l'exe nello Scripts globale o in quello utente:
    # aggiungo entrambi se mancano
    dirs = {Path(sysconfig.get_path("scripts"))}
    scheme = ("nt_user" if pu.IS_WINDOWS
              else "osx_framework_user" if sys.platform == "darwin" else "posix_user")
    try:
        dirs.add(Path(sysconfig.get_path("scripts", scheme)))
    except Exception:
        pass
    dirs = [d for d in dirs if d.name]
    if pu.IS_WINDOWS:
        try:
            import ctypes
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_ALL_ACCESS) as k:
                try:
                    cur = winreg.QueryValueEx(k, "Path")[0]
                except FileNotFoundError:
                    cur = ""
            voci = [p.strip().lower() for p in cur.split(";") if p.strip()]
            mancanti = [d for d in dirs if str(d).lower() not in voci]
            if not mancanti:
                _ok("'ugo' gia' raggiungibile (" + ", ".join(str(d) for d in dirs) + ")")
                return
            new = cur
            for d in mancanti:
                new = (new.rstrip(";") + ";" if new else "") + str(d)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                    winreg.KEY_ALL_ACCESS) as k:
                winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, new)
            # notifica ai processi: i NUOVI terminali vedono subito il PATH
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
            _ok("aggiunto al PATH utente: " + ", ".join(str(d) for d in mancanti)
                + " (apri un nuovo terminale)")
        except Exception as exc:
            _warn(f"non posso toccare il PATH ({exc}); creo uno shim")
            _make_shim_windows()
    else:
        _make_shim_unix()


def _make_shim_windows() -> None:
    """Alternativa al PATH: ugo.bat (+ chicco.bat legacy) che chiama il python giusto."""
    target = Path.home() / ".local" / "bin"
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in ("ugo.bat", "chicco.bat"):
            bat = target / name
            bat.write_text(f'@echo off\r\n"{sys.executable}" -m ugo_agent.cli %*\r\n')
        _ok(f"shim creati: {target / 'ugo.bat'} + chicco.bat")
    except Exception as exc:
        _warn(f"shim non creato ({exc}); usa: \"{sys.executable}\" -m ugo_agent.cli")


def _make_shim_unix() -> None:
    """macOS/Linux: piccolo launcher shell in ~/.local/bin."""
    target = Path.home() / ".local" / "bin"
    try:
        target.mkdir(parents=True, exist_ok=True)
        sh = target / "ugo"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m ugo_agent.cli "$@"\n')
        sh.chmod(0o755)
        legacy = target / "chicco"
        legacy.write_text(sh.read_text())
        legacy.chmod(0o755)
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m ugo_agent.cli "$@"\n')
        sh.chmod(0o755)
        _ok(f"launcher creato: {sh}")
        if str(target) not in os.environ.get("PATH", ""):
            _warn(f"aggiungi al PATH nel tuo .zshrc/.bashrc:  export PATH=\"{target}:$PATH\"")
    except Exception as exc:
        _warn(f"launcher non creato ({exc}); usa: {sys.executable} -m ugo_agent.cli")


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
    print("Ugo setup — installo tutto il necessario\n")
    install_pip_deps()
    ensure_path()
    install_ollama()
    install_qwen()
    install_whisper()
    install_vosk()
    _step("Voce naturale Piper (TTS locale, ~85 MB)")
    from . import piper_tts
    _ok("voce naturale pronta") if piper_tts.install_sync() else _warn("si scarichera' al primo avvio")
    print("\nSetup completato. Avvia con:  ugo run")
    print("Se il terminale non trova 'ugo', aprine uno nuovo.")
    return 0


def cmd_log() -> int:
    """Apre un terminale che mostra in diretta cosa sente l'ascolto passivo."""
    try:
        from . import platform_utils as pu
    except ImportError:  # modulo top-level
        import platform_utils as pu
    log = pu.data_dir() / "passive_log.txt"
    if not log.exists():
        print("Nessun log: il widget non ha ancora ascoltato (prova: ugo run).")
        return 1
    if pu.IS_WINDOWS and shutil.which("powershell"):
        ps = ("$host.UI.RawUI.WindowTitle='Ugo - ascolto passivo'; "
              f"Get-Content -Path '{log}' -Wait -Tail 15")
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-NoExit", "-Command", ps],
                             creationflags=0x00000010)  # CREATE_NEW_CONSOLE
            print("Terminale aperto: vedi in diretta cosa sente Ugo (Ctrl+C o X per chiudere).")
            return 0
        except Exception:
            pass
    if pu.IS_MAC and shutil.which("osascript"):
        try:
            subprocess.run(["osascript", "-e",
                            f'tell application "Terminal" to do script "tail -f -n 15 {log}"'],
                           check=True)
            return 0
        except Exception:
            pass
    # fallback multipiattaforma: segue il file qui, nel terminale corrente
    print(f"--- cosa sente Ugo ({log}) — Ctrl+C per uscire ---")
    print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]))
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while True:
                ln = f.readline()
                if ln.endswith("\n"):
                    print(ln, end="", flush=True)
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_doctor() -> int:
    print("Ugo doctor — stato dell'installazione\n")
    checks = [
        ("Dipendenze Python (fastapi)", py_import("fastapi") and py_import("uvicorn")),
        ("Laya (intent)", py_import("laya")),
        ("Whisper large-v3-turbo (CT2)", _fw_file_present()),
        ("Vosk it", VOSK_DIR.is_dir()),
        ("Voce naturale Piper (Paola)", piper_tts.is_ready()),
        ("Ollama installato", OLLAMA_EXE.exists()),
        ("Ollama attivo", ollama_ok()),
        ("Qwen2.5 0.5B", qwen_ok()),
        ("Server Ugo attivo", server_up()),
    ]
    for name, okflag in checks:
        (_ok if okflag else _warn)(f"{name}")
    missing = [n for n, o in checks if not o]
    print()
    if missing:
        print("Mancanze: " + ", ".join(missing) + "\nRimedio con:  ugo setup")
    else:
        print("Tutto pronto! Avvia con:  ugo run")
    return 0


def cmd_run(only: str | None = None) -> int:
    # setup leggero: solo cio' che manca davvero
    install_pip_deps()
    ensure_path()
    if not (ollama_ok() and qwen_ok()):
        install_ollama()
        install_qwen()
    if not _fw_file_present() and os.environ.get("WHISPER", "1") == "1":
        install_whisper()
    if not VOSK_DIR.is_dir():
        install_vosk()

    try:
        from . import launcher
    except ImportError:
        from ugo_agent import launcher
    return launcher.start_all(reuse=True,
                              skip_widget=(only == "server"))


def cmd_stop() -> int:
    _step("Arresto")
    try:
        from . import launcher
    except ImportError:
        from ugo_agent import launcher
    launcher.stop_all()
    return 0


def cmd_help() -> int:
    """Aiuto completo: tutti i comandi con una spiegazione breve."""
    print("""Ugo — assistente vocale locale.  Uso:  ugo <comando>

Comandi:
  run [server]    avvia tutto: server + widget (prima installa cio' che manca);
                  con "server" avvia solo il server, senza widget desktop
  start           sinonimo di run
  setup           installa/verifica TUTTO (dipendenze, Ollama, modelli, voce)
  doctor          diagnostica: cosa e' installato e cosa manca, senza installare
  stop            ferma widget e server (uccide solo i processi di Ugo)
  log             apre un terminale che mostra in diretta l'ascolto passivo
                  (cosa sente la wake word "Ugo")
  update          controlla GitHub e aggiorna all'ultima versione del canale
  channel         canale di aggiornamento:  dev = main  |  stable = release
                  (solo  ugo channel  mostra quello attivo)
  version         versione installata
  help            questo aiuto

Prime volte:
  ugo setup       una volta sola, installa tutto
  ugo run         ogni volta che vuoi usare Ugo (o doppio click su ugo_app.py)

Esempi:
  ugo channel stable     solo release ufficiali (rollback facile)
  ugo run server         server senza widget (per la UI web nel browser)""")
    return 0


def main() -> int:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    if cmd in ("setup", "install"):
        return cmd_setup()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "stop":
        return cmd_stop()
    if cmd == "log":
        return cmd_log()
    if cmd == "update":
        try:
            from . import update
        except ImportError:
            from ugo_agent import update
        ch = update.get_channel()
        loc = update.local_version()
        if ch == "stable":
            rem = update.latest_tag() or "? (offline?)"
        else:
            rem = update.remote_version(timeout=5) or "? (offline?)"
        print(f"versione installata: {loc or '?'}   "
              f"su GitHub ({ch}): {rem}")
        if update.check_update(interactive=True):
            print("Riavvia i componenti:  ugo stop && ugo run")
        else:
            print("Niente da aggiornare.")
        return 0
    if cmd == "channel":
        try:
            from . import update
        except ImportError:
            from ugo_agent import update
        if len(sys.argv) > 2 and sys.argv[2].lower() in update.CHANNELS:
            update.set_channel(sys.argv[2].lower())
            print(f"canale di aggiornamento: {sys.argv[2].lower()}")
        else:
            cur = update.get_channel()
            print(f"canale di aggiornamento: {cur}   "
                  f"(alterna con:  ugo channel dev|stable)")
            if cur == "stable":
                tag = update.latest_tag()
                print("ultimo rilascio su GitHub: "
                      f"{tag or ('nessuno ancora' if tag == '' else 'offline?')}")
            else:
                print("aggiornamento: ultimo codice su main (repo privata OK)")
        return 0
    if cmd in ("version", "--version"):
        try:
            from . import update
        except ImportError:
            from ugo_agent import update
        print(f"ugo-agent {update.local_version() or '?'}")
        return 0
    if cmd in ("run", "start"):
        only = sys.argv[2].lower() if len(sys.argv) > 2 else None
        return cmd_run(only)
    if cmd in ("-h", "--help", "help"):
        return cmd_help()
    print(f"comando sconosciuto: {cmd}\n")
    return cmd_help()


if __name__ == "__main__":
    raise SystemExit(main())
