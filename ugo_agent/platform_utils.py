# -*- coding: utf-8 -*-
"""
Astrazioni multipiattaforma (Windows / macOS / Linux).

Ogni funzione incapsula una differenza di sistema operativo: il resto del
codice non fa mai branch su sys.platform direttamente. Su Windows il
comportamento e' IDENTICO a prima; i rami macOS/Linux sono scritti qui e
verificati in CI (install + import + compileall su runner reali).
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
OS_NAME = "windows" if IS_WINDOWS else "macos" if IS_MAC else "linux" if IS_LINUX else platform.system().lower()


# ---------------------------------------------------------------------------
# Cartelle utente e dati applicazione
# ---------------------------------------------------------------------------
_MIGRATED_MARKER = ".migrated_from_chicco"


def _legacy_data_dir() -> Path:
    """Vecchia cartella dati (pre-rinomina Ugo): source della migrazione."""
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "chicco"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "chicco"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "chicco"


def _migrate_legacy_data(new: Path) -> None:
    """Prima esecuzione post-rinomina: copia la vecchia cartella dati 'chicco'
    nella nuova 'ugo' (memoria refusi/alias, preferenze widget, indice app,
    voci Piper, log). Una sola volta: il marker evita le ricopie. La vecchia
    cartella NON viene cancellata (rollback immediato con la versione prima)."""
    try:
        old = _legacy_data_dir()
        if not old.is_dir() or not any(old.iterdir()):
            return                              # niente da migrare
        (new / _MIGRATED_MARKER).parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old, new, dirs_exist_ok=True)
        (new / _MIGRATED_MARKER).write_text(
            "migrazione dati da chicco eseguita il " + platform.platform())
    except Exception as exc:                    # mai bloccare l'avvio per questo
        print(f"[migr] copia dati non riuscita ({exc}): uso solo la nuova cartella")


def data_dir() -> Path:
    """Cartella dei file runtime (memoria, preferenze, voci Piper, log).
    Dalla rinomina a Ugo e' '<dati>/ugo'; al primo avvio copia al suo interno
    il contenuto della vecchia '<dati>/chicco' preservando memoria e prefs."""
    if IS_WINDOWS:
        d = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ugo"
    elif IS_MAC:
        d = Path.home() / "Library" / "Application Support" / "ugo"
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ugo"
    if not (d / _MIGRATED_MARKER).exists():
        _migrate_legacy_data(d)
    return d


def user_folders() -> dict:
    """Le cartelle utente usate dai comandi file (chiavi: desktop, documents, downloads, home)."""
    home = Path.home()
    folders = {"home": home}
    if IS_WINDOWS:
        folders["desktop"] = Path(os.environ.get("USERPROFILE", home)) / "Desktop"
        folders["documents"] = Path(os.environ.get("USERPROFILE", home)) / "Documents"
        folders["downloads"] = Path(os.environ.get("USERPROFILE", home)) / "Downloads"
    else:
        # XDG: su mac e nella maggior parte dei Linux i nomi coincidono
        folders["desktop"] = home / "Desktop"
        folders["documents"] = home / "Documents"
        folders["downloads"] = home / "Downloads"
    return {k: v for k, v in folders.items() if v.exists()} if len(folders) > 1 else folders


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
_tts_engine = None
_tts_lock = None


def _engine():
    global _tts_engine, _tts_lock
    if _tts_engine is None:
        import threading
        import pyttsx3
        _tts_engine = pyttsx3.init()
        _tts_lock = threading.Lock()
        if IS_WINDOWS:
            # la voce italiana (Elsa) resta la scelta preferita su Windows
            try:
                for v in _tts_engine.getProperty("voices"):
                    if "italy" in (v.name or "").lower() or "italian" in (v.id or "").lower():
                        _tts_engine.setProperty("voice", v.id)
                        break
            except Exception:
                pass
        elif IS_MAC:
            try:
                for v in _tts_engine.getProperty("voices"):
                    if "it-" in (v.id or "").lower() or "italian" in (v.name or "").lower():
                        _tts_engine.setProperty("voice", v.id)
                        break
            except Exception:
                pass
    return _tts_engine


def tts_say(text: str, wav_out: Path) -> None:
    """Sintetizza 'text' e salva il WAV (nuova istanza ogni volta: thread-safe)."""
    import pyttsx3
    eng = pyttsx3.init()
    try:
        prefs = []
        for v in eng.getProperty("voices"):
            vid, vname = (v.id or "").lower(), (v.name or "").lower()
            if ("italy" in vname or "italian" in vname or "it_" in vid or "it-" in vid):
                eng.setProperty("voice", v.id)
                break
        eng.save_to_file(text, str(wav_out))
        eng.runAndWait()
    finally:
        try:
            eng.stop()
        except Exception:
            pass


def tts_stop() -> None:
    """Interrompe la riproduzione vocale in corso."""
    if IS_WINDOWS:
        try:
            import winsound
            winsound.PlaySound(None, 0)
            return
        except Exception:
            pass
    global _tts_engine
    try:
        if _tts_engine is not None:
            _tts_engine.stop()
    except Exception:
        pass
    # su mac/Linux pyttsx3 parla via espeak/nsss in-process: stop() basta


def tts_play_file(wav_path: Path) -> None:
    """Riproduce un WAV in modo asincrono (il widget non ha bisogno del server)."""
    if IS_WINDOWS:
        import winsound
        winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        return
    try:
        if IS_MAC:
            subprocess.Popen(["afplay", str(wav_path)])
        else:
            subprocess.Popen(["aplay", str(wav_path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def press_media_keys(up_steps: int, down_steps: int) -> bool:
    """Premi i tasti multimediali volume su mac/Linux (fallback senza pycaw).
    Ritorna True se il sistema operativo e' supportato."""
    if not IS_LINUX:
        # mac non ha tasti multimediali via CLI; su Windows c'e' pycaw/tasti SendKeys
        return False
    try:
        if up_steps:
            subprocess.Popen(["sh", "-c",
                              f"for i in $(seq {up_steps}); do {pactl_or_alsa('up')}; done"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if down_steps:
            subprocess.Popen(["sh", "-c",
                              f"for i in $(seq {down_steps}); do {pactl_or_alsa('down')}; done"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def pactl_or_alsa(direction: str) -> str:
    """Comando singolo di aggiustamento volume su Linux (pactl se c'e', alsa altrimenti).
    direction: 'up' | 'down' | 'mute'."""
    if direction == "mute":
        if shutil.which("pactl"):
            return "pactl set-sink-mute @DEFAULT_SINK@ toggle"
        if shutil.which("amixer"):
            return "amixer -q set Master toggle"
        return "true"
    if shutil.which("pactl"):
        return f"pactl set-sink-volume @DEFAULT_SINK@ {'+' if direction == 'up' else '-'}2%"
    if shutil.which("amixer"):
        return f"amixer -q set Master {'5%+' if direction == 'up' else '5%-'}"
    return "true"


# ---------------------------------------------------------------------------
# Aprire file / app / URL
# ---------------------------------------------------------------------------
def open_path(target: str) -> None:
    """Apre un percorso col programma predefinito (equivalente di os.startfile)."""
    if IS_WINDOWS:
        os.startfile(target)  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


def open_app_bundle(name_or_path: str) -> bool:
    """Su macOS avvia un'app /Applications/<nome>.app; ritorna True se riuscito."""
    if not IS_MAC:
        return False
    p = Path(name_or_path)
    if not p.suffix:
        p = Path("/Applications") / f"{name_or_path}.app"
    if p.exists():
        subprocess.Popen(["open", str(p)])
        return True
    return False


def subprocess_creationflags() -> int:
    """Flag per spawnare processi senza console (solo Windows)."""
    if IS_WINDOWS:
        return subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    return 0


def popen_hidden(cmd: list, **kwargs) -> subprocess.Popen:
    """Popen che su Windows nasconde la finestra console; kwargs invariati altrove."""
    kwargs.setdefault("creationflags", subprocess_creationflags())
    return subprocess.Popen(cmd, **kwargs)


# ---------------------------------------------------------------------------
# Volume (pycaw su Windows; usato dal server solo dove disponibile)
# ---------------------------------------------------------------------------
def volume_step(direction: str) -> bool:
    """Regola il volume di sistema. Ritorna False se l'OS non e' supportato."""
    if IS_WINDOWS:
        try:
            from ctypes import cast, POINTER
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            dev = AudioUtilities.GetSpeakers()
            # pycaw recente: EndpointVolume gia' attivato; vecchie versioni: Activate()
            interface = getattr(dev, "EndpointVolume", None) or dev.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(interface, POINTER(IAudioEndpointVolume))
            step = 0.06 if direction == "up" else -0.06
            cur = vol.GetMasterVolumeLevelScalar()
            vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, cur + step)), None)
            return True
        except Exception:
            return False
    try:  # macOS / Linux: usare i tool di sistema
        if IS_MAC:
            script = "set volume output volume ((output volume of (get volume settings)) + 6)" \
                if direction == "up" else \
                "set volume output volume ((output volume of (get volume settings)) - 6)"
            subprocess.run(["osascript", "-e", script], check=True,
                           capture_output=True, timeout=5)
            return True
        tool = "pactl" if IS_LINUX else None
        if tool and shutil_which(tool):
            if direction == "up":
                subprocess.run([tool, "set-sink-volume", "@DEFAULT_SINK@", "+6%"],
                               check=True, capture_output=True, timeout=5)
            else:
                subprocess.run([tool, "set-sink-volume", "@DEFAULT_SINK@", "-6%"],
                               check=True, capture_output=True, timeout=5)
            return True
    except Exception:
        pass
    return False


def shutil_which(cmd: str):
    import shutil
    return shutil.which(cmd)


# ---------------------------------------------------------------------------
# Installazione dipendenze di sistema (usata dalla CLI)
# ---------------------------------------------------------------------------
def ollama_install_cmd() -> list | None:
    """Comando per installare Ollama sulla piattaforma, se determinabile."""
    if IS_WINDOWS:
        return ["winget", "install", "--id", "Ollama.Ollama", "--accept-source-agreements",
                "--accept-package-agreements", "--silent"]
    if IS_MAC:
        return ["brew", "install", "ollama"]
    return ["curl", "-fsSL", "https://ollama.com/install.sh", "|", "sh"]


def ollama_binary() -> str:
    """Nome/label del binario ollama per i controlli PATH."""
    return "ollama.exe" if IS_WINDOWS else "ollama"
