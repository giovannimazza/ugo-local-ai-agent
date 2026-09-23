# -*- coding: utf-8 -*-
"""Controllo versione su GitHub e auto-aggiornamento di Ugo.

Due canali di aggiornamento:
  - dev     (default): ultimo codice su main, controllo via git fetch (funziona
            anche con repo PRIVATE, usa le credenziali salvate)
  - stable: ultimo tag v* pubblicato come release — ugo update porta il codice
            esattamente a quel tag (rollback facile, nessuna sorpresa)

Comandi: `ugo channel` mostra/cambia canale; la scelta vive in
<data_dir>/update_channel.json. Il fallback per installazioni pip senza clone
confronta la versione nel pyproject remoto (solo repo pubbliche).
Offline o repo non raggiungibile: controllo saltato, mai un blocco.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

try:  # pacchetto (pip install / -m) O script diretto (python ugo_agent/...)
    from . import platform_utils as pu
except ImportError:
    if __package__ is None and str(Path(__file__).resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ugo_agent import platform_utils as pu

REPO_URL = "https://github.com/giovannimazza/ugo-local-ai-agent.git"
RAW_URL = ("https://raw.githubusercontent.com/giovannimazza/"
           "ugo-local-ai-agent/main/pyproject.toml")
ROOT = Path(__file__).resolve().parent.parent
CHANNEL_FILE = pu.data_dir() / "update_channel.json"
CHANNELS = ("dev", "stable")
DEFAULT_CHANNEL = "dev"
_VER_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)
_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def _ver_tuple(v: str) -> tuple:
    m = re.match(r"(\d+(?:\.\d+)*)", v or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def local_version() -> str:
    """Versione installata: pyproject.toml (clone/editable) o metadati pip."""
    try:
        m = _VER_RE.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("ugo-agent")
    except Exception:
        return ""


def _git(*args: str, timeout: int = 15) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# canale di aggiornamento
# ---------------------------------------------------------------------------
def get_channel() -> str:
    try:
        c = json.loads(CHANNEL_FILE.read_text(encoding="utf-8")).get("channel", "")
    except Exception:
        c = ""
    return c if c in CHANNELS else DEFAULT_CHANNEL


def set_channel(name: str) -> str:
    if name not in CHANNELS:
        raise ValueError(f"canale sconosciuto: {name} (validi: {', '.join(CHANNELS)})")
    CHANNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHANNEL_FILE.write_text(json.dumps({"channel": name}), encoding="utf-8")
    return name


def remote_version(timeout: float = 2.5) -> str:
    """Versione pubblicata su GitHub ('' se non raggiungibile; solo repo pubbliche)."""
    try:
        with urllib.request.urlopen(RAW_URL, timeout=timeout) as r:
            m = _VER_RE.search(r.read().decode("utf-8", "replace"))
        return m.group(1) if m else ""
    except Exception:
        return ""


def latest_tag(timeout: int = 20) -> str | None:
    """Ultimo tag v* su GitHub, in ordine di versione ('' se nessuno)."""
    if _git("fetch", "--quiet", "--tags", timeout=timeout) is None:
        return None  # offline / credenziali mancanti
    tags = _git("tag", "--list", "v*", "--sort=-v:refname") or ""
    return tags.splitlines()[0] if tags else ""


def _tag_version(tag: str) -> str:
    m = _TAG_RE.match(tag or "")
    return m.group(1) if m else ""


def commits_behind() -> int | None:
    """Commit di distacco da GitHub per un clone git (None se non calcolabile)."""
    if not (ROOT / ".git").is_dir():
        return None
    if _git("fetch", "--quiet", timeout=20) is None:
        return None  # offline o credenziali mancanti: salta il controllo
    n = _git("rev-list", "--count", "HEAD..@{u}")
    return int(n) if n is not None and n.isdigit() else None


def update_available() -> tuple[str, str] | None:
    """Informazioni sull'aggiornamento disponibile (None se aggiornati).

    canale dev:     confronto con main via commit di distacco (repo private OK)
    canale stable:  confronto versione locale vs ultimo tag v* pubblicato
    """
    loc = local_version()
    if get_channel() == "stable":
        tag = latest_tag()
        if tag is None:
            return None  # offline: niente controllo
        rem = _tag_version(tag)
        if loc and rem and _ver_tuple(rem) > _ver_tuple(loc):
            return loc, f"{tag} (canale stable)"
        return None
    # --- canale dev (comportamento storico) ---
    behind = commits_behind()
    if behind:  # >0: siamo indietro; None: non un clone / offline
        shown = loc or (_git("rev-parse", "--short", "HEAD") or "?")
        rem = _git("rev-parse", "--short", "@{u}") or "GitHub"
        return shown, f"{rem} ({behind} commit indietro)"
    if behind == 0:  # clone aggiornato
        return None
    # non-clone: confronto versioni via raw (richiede repo pubblica)
    rem = remote_version()
    if loc and rem and _ver_tuple(rem) > _ver_tuple(loc):
        return loc, rem
    return None


def apply_update() -> bool:
    """Aggiorna il codice: git pull (se e' un clone) + reinstall del pacchetto.

    Nel canale stable il pull porta esattamente all'ultimo tag (detached HEAD
    con checkout v*, niente tracking branch da gestire).
    Su un'installazione pip diretta (senza clone locale) reinstalla dalla repo.
    """
    def run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True)

    ok = True
    if (ROOT / ".git").is_dir():
        stashed = False
        if _git("status", "--porcelain") is not None and _git("status", "--porcelain"):
            # modifiche locali: le meto da parte per il pull e le rimetto dopo
            if _git("stash", "--include-untracked", "-q") is not None:
                stashed = True
        if get_channel() == "stable":
            tag = latest_tag()
            if tag:
                r = run(["git", "-C", str(ROOT), "checkout", tag])
                print((r.stdout or r.stderr).strip())
                ok &= r.returncode == 0
        else:
            r = run(["git", "-C", str(ROOT), "pull", "--ff-only"])
            print((r.stdout or r.stderr).strip())
            ok &= r.returncode == 0
        if stashed:
            _git("stash", "pop", "-q")  # le modifiche locali tornano
    r = run([sys.executable, "-m", "pip", "install", "-e", str(ROOT), "--quiet"])
    if r.returncode != 0:  # non-editable: installa direttamente dalla repo
        r = run([sys.executable, "-m", "pip", "install", "--upgrade",
                 "--force-reinstall", "--no-deps", REPO_URL])
    ok &= r.returncode == 0
    return ok


def check_update(interactive: bool = True) -> bool:
    """Controlla e aggiorna se serve. Ritorna True se ha aggiornato.

    interactive=True  -> chiede conferma a terminale (ugo run)
    interactive=False -> aggiorna in automatico (widget/doppio click)
    """
    try:
        av = update_available()
        if not av:
            return False
        loc, rem = av
        print(f"[aggiornamento] nuova versione su GitHub: {rem} "
              f"(installata: {loc})")
        if interactive:
            try:
                ans = input("Aggiornare ora? [s/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if not ans.startswith("s"):
                os.environ["CHICCO_NO_UPDATE"] = "1"  # non richiederlo piu'
                return False
        print("[aggiornamento] aggiorno...")
        if apply_update():
            print(f"[aggiornamento] fatto. Riavvio i componenti col codice nuovo.")
            return True
        print("[aggiornamento] fallito: proseguo con la versione corrente.")
    except Exception as exc:
        print(f"[aggiornamento] controllo non disponibile ({exc})")
    return False


def auto_check_and_restart() -> None:
    """Punto di controllo unico all'avvio: se su GitHub c'e' una versione
    piu' recente, aggiorna e rilancia il processo con il codice nuovo.
    Interattivo se c'e' un terminale, silenzioso altrimenti (doppio click).
    """
    if os.environ.get("CHICCO_UPDATED") == "1":
        return  # siamo gia' stati riavviati dopo un aggiornamento
    try:
        interactive = sys.stdout is not None and sys.stdout.isatty()
    except Exception:
        interactive = False
    if check_update(interactive=interactive):
        os.execve(sys.executable, [sys.executable] + sys.argv,
                  {**os.environ, "CHICCO_UPDATED": "1"})
