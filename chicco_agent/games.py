# -*- coding: utf-8 -*-
"""
Librerie di giochi dei launcher: Steam, Epic, GOG, Riot, EA, Ubisoft, Battle.net.

Legge i manifest NATIVI dei launcher (appmanifest_*.acf per Steam, *.item per
Epic, goggame-*.info per GOG, ecc.): sono gli elenchi veri di cio' che risulta
installato, non solo i collegamenti del menu Start.

Restituisce voci uniformi: {"launcher", "name", "install_dir"}.
Cache su disco con TTL: i manifest cambiano raramente.
"""
import json
import os
import re
import time
from pathlib import Path

CACHE_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "chicco" / "games_index.json"
TTL = 3600  # riscansiona al piu' ogni ora

_cache: dict = {"when": 0.0, "games": []}


def _steam_libraries(steam_root: Path) -> list:
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
            p = Path(m.group(1).replace("\\\\", "\\"))
            if p.is_dir():
                libs.append(p)
    return libs


def _collect_steam() -> list:
    root = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            root = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except Exception:
        root = Path("C:/Program Files (x86)/Steam")
    if not root.is_dir():
        return []
    games = []
    for lib in _steam_libraries(root):
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            txt = acf.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'"name"\s+"([^"]+)"', txt)
            if not m:
                continue
            name = m.group(1).strip()
            # toolkit/ridistribuibili: non sono giochi
            if re.match(r"(?i)steamworks|proton |steam linux", name):
                continue
            games.append({"launcher": "steam", "name": name, "install_dir": str(lib / "steamapps" / "common" / name)})
    return games


def _collect_epic() -> list:
    out = []
    base = Path(os.environ.get("PROGRAMDATA", "")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if base.is_dir():
        for it in base.glob("*.item"):
            try:
                d = json.loads(it.read_text(encoding="utf-8", errors="ignore"))
                if d.get("DisplayName") and d.get("LaunchExecutable"):
                    out.append({"launcher": "epic", "name": d["DisplayName"],
                                "install_dir": d.get("InstallLocation", "")})
            except Exception:
                pass
    return out


def _collect_gog() -> list:
    out = []
    for env in ("PROGRAMDATA", "PROGRAMFILES(X86)"):
        base = os.environ.get(env)
        if not base:
            continue
        for gdir in (Path(base) / "GOG.com" / "Games", Path(base) / "GOG Galaxy" / "Games"):
            if gdir.is_dir():
                for info in gdir.glob("goggame-*.info"):
                    try:
                        d = json.loads(info.read_text(encoding="utf-8", errors="ignore"))
                        name = (d.get("game", {}) or {}).get("name") or d.get("name")
                        if name:
                            out.append({"launcher": "gog", "name": name, "install_dir": str(info.parent)})
                    except Exception:
                        pass
    return out


def _collect_launcher_lnks() -> list:
    """I launcher senza libreria leggibile (Riot, EA, Ubisoft, Battle.net):
    segnalo solo il launcher stesso, prendendolo dai .lnk del menu Start."""
    wanted = {"riot games": "Riot Client", "league of legends": "League of Legends",
              "ea games": "EA app", "electronic arts": "EA app",
              "ubisoft": "Ubisoft Connect", "battle.net": "Battle.net"}
    found: dict = {}
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        prog = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if not prog.is_dir():
            continue
        for d in prog.iterdir():
            if not d.is_dir():
                continue
            dl = d.name.lower()
            for key, label in wanted.items():
                if key in dl and key not in found:
                    lnks = list(d.rglob("*.lnk"))
                    real = [f for f in lnks if not f.stem.lower().startswith("uninstall")]
                    if real:
                        found[key] = {"launcher": "launcher", "name": label,
                                      "install_dir": str(real[0])}
    return list(found.values())


def collect() -> list:
    games = _collect_steam() + _collect_epic() + _collect_gog() + _collect_launcher_lnks()
    seen, out = set(), []
    for g in games:
        key = (g["launcher"], g["name"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return sorted(out, key=lambda g: (g["launcher"], g["name"].lower()))


def get_games(force: bool = False) -> list:
    now = time.time()
    if force or not _cache["games"] or now - _cache["when"] > TTL:
        try:
            _cache["games"] = collect()
            _cache["when"] = now
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(_cache["games"], ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return _cache["games"]


LAUNCHER_LABEL = {"steam": "Steam", "epic": "Epic Games", "gog": "GOG",
                  "launcher": "launcher installato"}


def by_launcher(launcher: str) -> list:
    l = launcher.lower()
    return [g for g in get_games() if g["launcher"] == l]


def known_launchers() -> list:
    return sorted({g["launcher"] for g in get_games()})
