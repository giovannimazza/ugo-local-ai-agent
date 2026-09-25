# -*- coding: utf-8 -*-
"""
Widget desktop flottante dell'assistente vocale (Ugo).

Finestrella senza barra titolo, sempre in primo piano, TRASCINABILE ovunque:
  - cerchio microfono: 1 click = registra, 2o click = invia
    (in hover si gonfia come una bollicina, alla pressione si schiaccia,
     durante la registrazione pulsa un anello rosso)
  - in hover si apre una card compatta: nome Ugo, tasto T per scrivere,
    tre puntini per le impostazioni e X per richiuderla
  - bolla di risposta arrotondata che compare/svanisce in dissolvenza + voce TTS
  - doppio click sul cerchio = scorciatoia per le impostazioni
  - click destro sul cerchio = chiudi il widget

Avvio consigliato (niente console):  pythonw assistant_widget.py
Se il server non e' attivo, lo avvia da solo (in background, la UI appare subito).

Nota tecnica: su Windows la trasparenza a colore-chiave (-transparentcolor)
NON supporta l'alpha parziale. Tutti gli effetti "lucidi" (gradienti, riflessi,
ombre dell'icona) sono quindi disegnati DENTRO le forme opache con Pillow;
sul bordo esterno un anello scuro sottile maschera la scalettatura.
"""
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
import soundcard as sc
import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

try:
    from . import platform_utils as pu
except ImportError:  # avviato come script diretto
    if __package__ is None and str(Path(__file__).resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ugo_agent import platform_utils as pu

# DEVE avvenire PRIMA di creare qualsiasi finestra: se il processo non si
# dichiara "DPI aware", su un monitor con scaling (125%/150%/200%, il caso
# piu' comune oggi) Windows non lascia disegnare l'app alla risoluzione
# fisica ma la fa renderizzare a bassa risoluzione e poi INGRANDISCE quel
# bitmap gia' composito con un filtro di stretch. Per una finestra a
# colore-chiave (-transparentcolor) questo produce esattamente l'alone/
# anello sdoppiato e i pixel "sporchi" che si vedono attorno al cerchio:
# non e' un problema di anti-aliasing nel disegno, e' lo stretch dell'OS
# applicato DOPO che il colore-chiave e' gia' stato fissato.
if pu.IS_WINDOWS:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

PKG_DIR = Path(__file__).resolve().parent
BASE = pu.data_dir()
BASE.mkdir(parents=True, exist_ok=True)

# i crash silenziosi sono impossibili da diagnosticare (pythonw non mostra
# nulla): ogni traceback imprevisto finisce in widget_crash.log accanto alle prefs
def _log_crash(tp, val, tb):
    import traceback
    try:
        (BASE / "widget_crash.log").open("a", encoding="utf-8").write(
            "\n=== " + time.strftime("%Y-%m-%d %H:%M:%S") + " ===\n"
            + "".join(traceback.format_exception(tp, val, tb)))
    except Exception:
        pass
    sys.__excepthook__(tp, val, tb)


sys.excepthook = _log_crash


def _log_tk_callback(tp, val, tb):
    """Le eccezioni nei callback Tk non passano da sys.excepthook: senza
    questo hook muoiono in silenzio (pythonw non mostra nulla) e un bug
    diventa solo 'il tasto non funziona'."""
    _log_crash(tp, val, tb)


sys.report_callback_exception = _log_tk_callback
PORT = 8123
SR = 16000
POS_FILE = BASE / "widget_pos.json"
ROOT_URL = f"http://127.0.0.1:{PORT}"

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
ACCENT, RED = "#e8963c", "#e5484d"          # arancione (come la UI web), rosso registrazione
CARD, TXT, MUT = "#151827", "#e8e9f3", "#8b90ad"
PILL_BG = "#191d30"      # riempimento pillola = sfondo della Entry (devono coincidere)
PILL_EDGE = "#2e3350"    # bordo sottile della pillola e della bolla
SPK_BG = "#262b46"       # pulsante altoparlante (voce attiva)
TRANSPARENT = "#010101"  # colore-chiave: invisibile e click-through su Windows
_KEY_RGB = (1, 1, 1)

# ---------------------------------------------------------------------------
# Layout (tutto in pixel, finestra a dimensione FISSA: le zone vuote sono
# trasparenti e click-through, quindi non danno fastidio)
# ---------------------------------------------------------------------------
C = 100                  # lato del canvas del cerchio
BD = 52                  # diametro del cerchio a riposo (richiesta utente: piu' piccolo)
HOVER_SCALE = 1.10       # quanto si gonfia in hover
PRESS_SCALE = 0.92       # quanto si schiaccia alla pressione
DRAG_SCALE = 1.05        # "sollevato" mentre lo trascini
S_MIN, S_MAX = 0.88, 1.18
PULSE_N, PULSE_MS = 14, 1200   # fotogrammi e durata dell'anello di registrazione
HALO_N, HALO_MS = 20, 2000     # anello "respirante" dell'ascolto passivo

PANEL_W, PANEL_H = 216, 130    # card espansa: riga comandi a y=94, sotto il cerchio
                               # (il tasto passivo al centro non tocca l'orb)
PANEL_R = 25
ENTRY_W, ENTRY_H = 176, 32     # pillola della textbox, aperta dal tasto T
SEND_D, SEND_D_HOVER = 24, 27  # tasto invia a riposo / in hover
SPK_D = 26                     # tasto mute TTS
LISTEN_D = 26                  # tasto on/off ascolto passivo (wake word)
CLOSE_D = 22                   # X per richiudere la card
GAP = 6

# A riposo si vede solo il cerchio; in hover la card occupa questa stessa
# finestra. Il microfono resta centrato, come il mock-up di riferimento.
WIN_W, WIN_H = PANEL_W, PANEL_H
CIRCLE_CX, CIRCLE_CY = PANEL_W // 2, 58   # orb rialzato: la riga comandi gli sta sotto
MIC_X, MIC_Y = CIRCLE_CX - C // 2, CIRCLE_CY - C // 2
PILL_X, PILL_Y = (PANEL_W - ENTRY_W) // 2, 76

SS = 6  # supersampling per l'anti-alias (piu' alto = bordi piu' lisci,
        # importante perche' il colore-chiave di Windows taglia l'alpha in
        # modo binario: senza abbastanza supersampling gli anelli sottili
        # come l'alone dell'ascolto passivo mostrano una scalettatura visibile)


# ---------------------------------------------------------------------------
# Preferenze (posizione + mute) — scrittura SEMPRE in merge
# ---------------------------------------------------------------------------
def _load_prefs() -> dict:
    try:
        return json.loads(POS_FILE.read_text()) if POS_FILE.exists() else {}
    except Exception:
        return {}


def _save_prefs(**changes) -> None:
    prefs = _load_prefs()
    prefs.update(changes)
    try:
        prefs["x"], prefs["y"] = root.winfo_x(), root.winfo_y()
    except Exception:
        pass
    prefs["v"] = 3
    try:
        POS_FILE.write_text(json.dumps(prefs))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ROOT_URL}/", timeout=2):
            return True
    except Exception:
        return False


def ensure_server() -> None:
    if server_up():
        return
    pu.popen_hidden([sys.executable, str(PKG_DIR / "server.py")],
                    cwd=str(PKG_DIR.parent))
    for _ in range(60):
        time.sleep(1)
        if server_up():
            return


# ---------------------------------------------------------------------------
# Rendering con Pillow
# ---------------------------------------------------------------------------
def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) if isinstance(c, str) else c


def _mix(a, b, t):
    a, b = _hex(a), _hex(b)
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _lighter(c, t):
    return _mix(c, (255, 255, 255), t)


def _darker(c, t):
    return _mix(c, (0, 0, 0), t)


def _css(c):
    """Tupla RGB -> stringa colore Tk (itemconfig non accetta tuple)."""
    return c if isinstance(c, str) else "#%02x%02x%02x" % tuple(_hex(c))


def _vgrad(w, h, top, bottom) -> Image.Image:
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    arr = np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t
    arr = np.broadcast_to(arr, (h, w, 3))
    return Image.fromarray(arr.round().astype(np.uint8), "RGB")


def _ramp(w, h, y0, y1, a0, a1) -> Image.Image:
    """Maschera L verticale: a0 in y0 -> a1 in y1 (costante fuori)."""
    y = np.arange(h, dtype=np.float32)
    t = np.clip((y - y0) / max(1.0, y1 - y0), 0, 1)
    col = a0 + (a1 - a0) * t
    return Image.fromarray(np.broadcast_to(col[:, None], (h, w)).round().astype(np.uint8), "L")


def _overlay(img, color, mask) -> None:
    layer = Image.new("RGBA", img.size, tuple(color) + (0,))
    layer.putalpha(mask)
    img.alpha_composite(layer)


def _key(img: Image.Image, thr: int = 110, base=None) -> Image.Image:
    """RGBA -> RGB con colore-chiave: niente alpha parziale (limite di Windows).

    Con base=(r,g,b) i buchi trasparenti prendono QUEL colore invece del
    colore-chiave: per le immagini che stanno sempre sopra la card (pillola
    della textbox) cosi' gli angoli non mostrano il desktop sotto.
    """
    a = np.asarray(img.convert("RGBA"))
    out = a[..., :3].copy()
    holes = a[..., 3] < thr
    out[holes] = _hex(base) if base is not None else _KEY_RGB
    # un pixel opaco che per caso coincide con la chiave diventerebbe un buco
    clash = (~holes) & np.all(out == _KEY_RGB, axis=-1)
    out[clash] = (2, 2, 2)
    return Image.fromarray(out, "RGB")


def _premul_resize(img: Image.Image, size) -> Image.Image:
    """Ridimensiona un'immagine RGBA senza scurire il bordo verso il nero.

    Image.resize normale interpola RGB e alpha in modo indipendente: un
    pixel di bordo (alpha basso) viene mischiato con l'RGB (0,0,0) dello
    sfondo trasparente, scurendo leggermente il contorno. Premoltiplicando
    prima del resize e dividendo dopo si evita l'effetto.
    """
    r, g, b, a = img.split()
    rgb = np.asarray(Image.merge("RGB", (r, g, b)), dtype=np.float32)
    av = np.asarray(a, dtype=np.float32) / 255.0
    premul = (rgb * av[..., None]).astype(np.uint8)
    premul_small = Image.fromarray(premul, "RGB").resize(size, Image.LANCZOS)
    a_small = a.resize(size, Image.LANCZOS)
    pr = np.asarray(premul_small, dtype=np.float32)
    ar = np.asarray(a_small, dtype=np.float32) / 255.0
    out_rgb = np.clip(pr / np.clip(ar[..., None], 1e-3, 1.0), 0, 255).astype(np.uint8)
    return Image.fromarray(np.dstack([out_rgb, np.asarray(a_small)]), "RGBA")


def _rline(d, pts, width, fill=255) -> None:
    """Linea con estremi e giunti arrotondati."""
    d.line(pts, fill=fill, width=int(width), joint="curve")
    r = width / 2
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _glossy_ss(n: int, base, icon=None) -> Image.Image:
    """Bottone tondo lucido, supersampled (lato n). icon(draw, n) disegna in una maschera L."""
    b = _hex(base)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([0, 0, n - 1, n - 1], fill=_darker(b, 0.45) + (255,))
    e = SS  # 1 px di bordo scuro: definisce la forma su qualsiasi sfondo
    body = Image.new("L", (n, n), 0)
    ImageDraw.Draw(body).ellipse([e, e, n - 1 - e, n - 1 - e], fill=255)
    img.paste(_vgrad(n, n, _lighter(b, 0.24), _darker(b, 0.22)), (0, 0), body)
    # leggera ombra interna in basso (volume)
    low = ImageChops.multiply(body, _ramp(n, n, n * 0.55, n, 0, 55))
    _overlay(img, (0, 0, 0), low)

    if icon is not None:
        mask = Image.new("L", (n, n), 0)
        icon(ImageDraw.Draw(mask), n)
        shadow = Image.new("L", (n, n), 0)
        shadow.paste(mask, (0, int(SS * 1.2)))
        shadow = shadow.filter(ImageFilter.GaussianBlur(SS * 1.1)).point(lambda v: int(v * 0.45))
        _overlay(img, (0, 0, 0), ImageChops.multiply(shadow, body))
        _overlay(img, (255, 255, 255), mask)

    # riflesso sul bordo superiore
    rim = Image.new("L", (n, n), 0)
    ImageDraw.Draw(rim).ellipse([e, e, n - 1 - e, n - 1 - e], outline=255, width=max(1, int(SS * 1.2)))
    _overlay(img, (255, 255, 255), ImageChops.multiply(rim, _ramp(n, n, 0, n * 0.55, 150, 0)))
    # gloss "a lente" nella meta' alta
    g = Image.new("L", (n, n), 0)
    ImageDraw.Draw(g).ellipse([n * 0.16, n * 0.05, n * 0.84, n * 0.52], fill=255)
    _overlay(img, (255, 255, 255), ImageChops.multiply(g, _ramp(n, n, n * 0.05, n * 0.52, 88, 0)))
    return img


def _glossy(d: int, base, icon=None) -> Image.Image:
    return _premul_resize(_glossy_ss(d * SS, base, icon), (d, d))


def _mic_orb_ss(n: int, tone, icon) -> Image.Image:
    """Cerchio microfono in stile card: disco scuro sobrio, icona colorata.

    `tone` e' il COLORE DELL'ICONA (ACCENT a riposo, RED in registrazione):
    il disco segue la palette della card (SPK_BG con bordo scuro e riflesso
    leggeri, come i tasti T/puntini) invece di essere una pallina arancione
    che stona con tutto il resto.
    """
    b = _hex(SPK_BG)
    t = _hex(tone)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    edge = max(SS, int(n * 0.03))
    # anello esterno scuro: definisce la forma su qualsiasi sfondo
    d.ellipse([0, 0, n - 1, n - 1], fill=_darker(b, 0.5) + (255,))

    body = Image.new("L", (n, n), 0)
    ImageDraw.Draw(body).ellipse([edge, edge, n - 1 - edge, n - 1 - edge], fill=255)
    img.paste(_vgrad(n, n, _lighter(b, 0.22), _darker(b, 0.18)), (0, 0), body)

    # riflesso sul bordo superiore, sobrio come sulla card
    rim = Image.new("L", (n, n), 0)
    ImageDraw.Draw(rim).ellipse([edge, edge, n - 1 - edge, n - 1 - edge],
                                outline=255, width=max(1, int(SS * 1.2)))
    _overlay(img, (255, 255, 255), ImageChops.multiply(rim, _ramp(n, n, 0, n * 0.5, 60, 0)))

    # icona colorata con ombra morbida sotto
    mask = Image.new("L", (n, n), 0)
    icon(ImageDraw.Draw(mask), n)
    shadow = mask.filter(ImageFilter.GaussianBlur(SS * 1.2))
    shifted = Image.new("L", (n, n), 0)
    shifted.paste(shadow, (0, int(SS * 1.4)))
    _overlay(img, (0, 0, 0),
             ImageChops.multiply(shifted, body).point(lambda v: int(v * 0.5)))
    _overlay(img, t, mask)
    return img


# --- icone (disegnate su maschera; stile Lucide: tratto uniforme, cap tondi) ---
def _icon_mic_body(d, n):
    """Glifo mic PIENO alla Material: capsula solida + arco che parte DAI
    FIANCHI della capsula e gira sotto (niente montanti/cornetti laterali,
    erano il difetto sempre visibile) + gambo e base.
    """
    u = n / 24.0
    lw = max(2, 2.0 * u)
    # capsula solida (9.4..14.6 x, 3..12.5 y)
    d.rounded_rectangle([9.4 * u, 3 * u, 14.6 * u, 12.5 * u], radius=2.6 * u, fill=255)
    # arco: centro (12,11), raggio 7 -> le estremita' stanno ai fianchi
    # della capsula (y=11), il fondo tocca y=18; nessun pezzo aggiunto
    d.arc([5 * u, 4 * u, 19 * u, 18 * u], start=0, end=180, fill=255, width=int(lw))
    # gambo dal fondo dell'arco e base
    _rline(d, [(12 * u, 18 * u), (12 * u, 21.5 * u)], lw)
    _rline(d, [(8.4 * u, 21.5 * u), (15.6 * u, 21.5 * u)], lw)


def _icon_mic(d, n):
    """Microfono attivo (stesso glifo del corpo)."""
    _icon_mic_body(d, n)


def _icon_send(d, n):
    cx = cy = n / 2
    lw = n * 0.115
    _rline(d, [(cx, cy + n * 0.21), (cx, cy - n * 0.19)], lw)
    _rline(d, [(cx - n * 0.17, cy - n * 0.02), (cx, cy - n * 0.19), (cx + n * 0.17, cy - n * 0.02)], lw)


def _icon_mic_on(d, n):
    """Microfono libero (senza barra): ascolto passivo ATTIVO."""
    _icon_mic_body(d, n)


def _icon_mic_off(d, n):
    """Microfono barrato: ascolto passivo SPENTO."""
    _icon_mic_body(d, n)
    cx = cy = n / 2
    _rline(d, [(cx - n * 0.30, cy - n * 0.30), (cx + n * 0.30, cy + n * 0.30)], n * 0.085)


def _icon_speaker(muted):
    def draw(d, n):
        cx = cy = n / 2
        bx = cx - n * 0.27
        bw = n * 0.12
        d.rounded_rectangle([bx, cy - n * 0.10, bx + bw, cy + n * 0.10], radius=n * 0.03, fill=255)
        d.polygon([(bx + bw - 1, cy - n * 0.11), (bx + bw + n * 0.15, cy - n * 0.26),
                   (bx + bw + n * 0.15, cy + n * 0.26), (bx + bw - 1, cy + n * 0.11)], fill=255)
        if muted:
            _rline(d, [(cx + n * 0.08, cy - n * 0.10), (cx + n * 0.28, cy + n * 0.10)], n * 0.075)
            _rline(d, [(cx + n * 0.08, cy + n * 0.10), (cx + n * 0.28, cy - n * 0.10)], n * 0.075)
        else:
            wx = bx + bw + n * 0.17
            for rr in (0.12, 0.21):
                r = n * rr
                d.arc([wx - r, cy - r, wx + r, cy + r], start=-50, end=50, fill=255, width=int(n * 0.065))
    return draw


def _icon_close(d, n):
    """X sottile, stile pillola 'yapper' in alto a destra."""
    cx = cy = n / 2
    r, lw = n * 0.22, n * 0.10
    _rline(d, [(cx - r, cy - r), (cx + r, cy + r)], lw)
    _rline(d, [(cx - r, cy + r), (cx + r, cy - r)], lw)


def _rounded_panel(w, h, radius, fill, edge, send_d=None) -> Image.Image:
    """Pillola/bolla scura con bordo sottile e riflesso in alto (+ tasto invia opzionale)."""
    N, M, R = w * SS, h * SS, radius * SS
    img = Image.new("RGBA", (N, M), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, N - 1, M - 1], radius=R, fill=_hex(edge) + (255,))
    d.rounded_rectangle([SS, SS, N - 1 - SS, M - 1 - SS], radius=max(1, R - SS), fill=_hex(fill) + (255,))
    rim = Image.new("L", (N, M), 0)
    ImageDraw.Draw(rim).rounded_rectangle([SS, SS, N - 1 - SS, M - 1 - SS], radius=max(1, R - SS),
                                          outline=255, width=SS)
    _overlay(img, (255, 255, 255), ImageChops.multiply(rim, _ramp(N, M, 0, min(M * 0.5, 18 * SS), 70, 0)))
    if send_d:
        btn = _glossy_ss(send_d * SS, ACCENT, _icon_send)
        cx, cy = N - M / 2, M / 2  # concentrico al tappo destro
        img.alpha_composite(btn, (int(round(cx - btn.width / 2)), int(round(cy - btn.height / 2))))
    return _premul_resize(img, (w, h))


# ---------------------------------------------------------------------------
# Finestra
# ---------------------------------------------------------------------------
root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
if pu.IS_WINDOWS:
    root.attributes("-transparentcolor", TRANSPARENT)
else:
    # mac/Linux: niente colore-chiave; finestra semi-trasparente e sfondo card
    root.attributes("-alpha", 0.92)
    TRANSPARENT = CARD
root.configure(bg=TRANSPARENT)

_families = set(tkfont.families(root))
_UI_FONTS = (("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI") if pu.IS_WINDOWS
             else ("SF Pro Text", "Helvetica Neue", "Ubuntu", "Cantarell", "DejaVu Sans")
             if not pu.IS_WINDOWS else ())
UI_FAMILY = next((f for f in _UI_FONTS if f in _families), "TkDefaultFont")
FONT_UI = (UI_FAMILY, 10)
FONT_MUT = (UI_FAMILY, 8)

prefs = _load_prefs()
x, y = prefs.get("x"), prefs.get("y")
if x is None or y is None:
    # cerchio in basso a destra, con card interamente nello schermo in hover
    x, y = root.winfo_screenwidth() - WIN_W - 24, root.winfo_screenheight() - WIN_H - 120
    prefs["v"] = 4
elif prefs.get("v") != 4:
    # Nelle versioni precedenti il punto salvato era l'angolo di una finestra
    # molto piu' larga e il microfono stava a x=244/y=50. Conservo quindi la
    # posizione percepita del microfono e limito la nuova card allo schermo.
    x = x + 244 - CIRCLE_CX
    y = y + 50 - CIRCLE_CY
    x = max(0, min(root.winfo_screenwidth() - WIN_W, x))
    y = max(0, min(root.winfo_screenheight() - WIN_H, y))
    prefs["v"] = 4
root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

# code di aggiornamento UI: tkinter NON e' thread-safe, i thread di rete e
# registrazione passano di qui invece di toccare i widget direttamente
_uiq: "queue.Queue" = queue.Queue()


def ui(fn):
    _uiq.put(fn)


def _pump_ui():
    try:
        while True:
            fn = _uiq.get_nowait()
            try:
                fn()
            except Exception:
                pass
    except queue.Empty:
        pass
    root.after(30, _pump_ui)


# --- bolla di risposta (arrotondata, dissolvenza) -----------------------------
class Bubble:
    MAXW, PADX, PADY, RADIUS = 260, 14, 10, 14

    def __init__(self):
        self.win = None
        self.cv = None
        self.timer = None
        self.fade_job = None
        self.dots_job = None
        self.alpha = 0.0
        self.visible = False
        self._imgs = {}

    def _build(self):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        if pu.IS_WINDOWS:
            self.win.attributes("-transparentcolor", TRANSPARENT)
        else:
            self.win.attributes("-alpha", 0.95)
        self.win.attributes("-alpha", 0.0)
        self.win.configure(bg=TRANSPARENT)
        self.cv = tk.Canvas(self.win, bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.cv.pack()
        self.bg_item = self.cv.create_image(0, 0, anchor="nw")
        self.txt_item = self.cv.create_text(self.PADX, self.PADY, anchor="nw", fill=TXT,
                                            font=FONT_UI, width=self.MAXW)
        self.raw_item = self.cv.create_text(self.PADX, self.PADY, anchor="nw", fill=MUT,
                                            font=FONT_MUT, width=self.MAXW)
        self.cv.bind("<Button-1>", lambda e: self.hide())  # click sulla bolla = chiudi
        self.win.withdraw()

    def _bg(self, w, h):
        k = (w, h)
        if k not in self._imgs:
            if len(self._imgs) > 40:
                self._imgs.clear()
            self._imgs[k] = ImageTk.PhotoImage(
                _key(_rounded_panel(w, h, self.RADIUS, PILL_BG, PILL_EDGE)))
        return self._imgs[k]

    def show(self, text, sticky=False, raw=None):
        if self.win is None:
            self._build()
        self._stop_dots()
        thinking = text.strip() == "…"
        self.cv.itemconfig(self.txt_item, text=("•••" if thinking else text))
        x0, y0, x1, y1 = self.cv.bbox(self.txt_item)
        if raw:
            # trascrizione originale corretta da Qwen (come nella UI web)
            self.cv.coords(self.raw_item, self.PADX, y1 + 5)
            self.cv.itemconfig(self.raw_item, text='\U0001F3A7 "' + raw + '"')
            rb = self.cv.bbox(self.raw_item)
            bh = (rb[3] - y0 + 2 * self.PADY) if rb else (y1 - y0 + 2 * self.PADY)
        else:
            self.cv.itemconfig(self.raw_item, text="")
            bh = y1 - y0 + 2 * self.PADY
        bw = max(48, x1 - x0 + 2 * self.PADX)
        if thinking:
            self.cv.itemconfig(self.txt_item, anchor="w", fill=MUT)
            self.cv.coords(self.txt_item, (bw - (x1 - x0)) / 2, bh / 2)
        else:
            self.cv.itemconfig(self.txt_item, anchor="nw", fill=TXT)
            self.cv.coords(self.txt_item, self.PADX, self.PADY)
        self.cv.itemconfig(self.bg_item, image=self._bg(bw, bh))
        self.cv.config(width=bw, height=bh)

        # sopra il cerchio, allineata al suo bordo destro; sotto se non c'e' spazio
        rx, ry = root.winfo_x(), root.winfo_y()
        bx = max(0, rx + CIRCLE_CX + BD // 2 + 4 - bw)
        by = ry + C // 2 - BD // 2 - bh - 6
        if by < 0:
            by = ry + WIN_H + 6
        self.win.geometry(f"{bw}x{bh}+{bx}+{by}")
        self.win.deiconify()
        if not self.visible:
            self.visible = True
            self._fade(0.97)
        elif self.fade_job:  # stava sparendo: torna su
            self._fade(0.97)

        # se la textbox era aperta, riporta il focus all'input
        if entry_frame.winfo_ismapped():
            try:
                root.focus_force()
                entry.focus_set()
            except Exception:
                pass
        if self.timer:
            try:
                root.after_cancel(self.timer)
            except Exception:
                pass
            self.timer = None
        if not sticky:
            self.timer = root.after(7000, self.hide)
        if thinking:
            self._dots(0)

    def hide(self):
        self._stop_dots()
        if self.win and self.visible:
            self.visible = False
            self._fade(0.0, then=self.win.withdraw)

    def _fade(self, target, then=None):
        if self.fade_job:
            root.after_cancel(self.fade_job)
            self.fade_job = None

        def step():
            delta = target - self.alpha
            self.alpha = target if abs(delta) < 0.12 else self.alpha + (0.14 if delta > 0 else -0.14)
            try:
                self.win.attributes("-alpha", self.alpha)
            except Exception:
                return
            if self.alpha != target:
                self.fade_job = root.after(16, step)
            else:
                self.fade_job = None
                if then:
                    then()
        step()

    def _dots(self, i):
        self.cv.itemconfig(self.txt_item, text="•" * (i % 3 + 1))
        self.dots_job = root.after(380, lambda: self._dots(i + 1))

    def _stop_dots(self):
        if self.dots_job:
            root.after_cancel(self.dots_job)
            self.dots_job = None


bubble = Bubble()

# --- card espansa --------------------------------------------------------------
# La card e' un solo canvas: quando non e' mappato il colore-chiave lascia
# passare il desktop e rimane soltanto il microfono. I controlli sono volutamente
# pochi e leggibili: T = scrivi, … = impostazioni, X = richiudi la card.
_PANEL_IMG = ImageTk.PhotoImage(_key(_rounded_panel(PANEL_W, PANEL_H, PANEL_R, PILL_BG, PILL_EDGE)))
shell_canvas = tk.Canvas(root, width=PANEL_W, height=PANEL_H, bg=TRANSPARENT,
                         highlightthickness=0, bd=0, cursor="arrow")
panel_item = shell_canvas.create_image(0, 0, anchor="nw", image=_PANEL_IMG)
shell_canvas.create_oval(20, 23, 25, 28, fill=ACCENT, outline="", tags="brand-dot")
shell_canvas.create_text(32, 25, anchor="w", text="Ugo", fill=TXT,
                         font=(UI_FAMILY, 10, "bold"), tags="brand")

# Tasto testo: il campo appare solo su richiesta, cosi' il widget resta pulito.
# Pillole di controllo con estremita' tonde costruite da rettangolo + due cerchi.
def _shell_pill(x0, y0, x1, y1, tag):
    r = (y1 - y0) / 2
    shell_canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=SPK_BG, outline="", tags=(tag, tag + "_bg"))
    shell_canvas.create_oval(x0, y0, x0 + 2 * r, y1, fill=SPK_BG, outline="", tags=(tag, tag + "_bg"))
    shell_canvas.create_oval(x1 - 2 * r, y0, x1, y1, fill=SPK_BG, outline="", tags=(tag, tag + "_bg"))


_shell_pill(18, 94, 52, 118, "type")
shell_canvas.create_text(35, 106, text="T", fill=TXT, font=(UI_FAMILY, 11, "bold"), tags="type")
_shell_pill(PANEL_W - 52, 94, PANEL_W - 18, 118, "settings")
shell_canvas.create_text(PANEL_W - 35, 104, text="•••", fill=TXT,
                         font=(UI_FAMILY, 10, "bold"), tags="settings")
shell_canvas.create_oval(PANEL_W - 39, 12, PANEL_W - 17, 34, fill=SPK_BG, outline="", tags=("close", "close_bg"))
shell_canvas.create_text(PANEL_W - 28, 23, text="×", fill=MUT,
                         font=(UI_FAMILY, 13, "bold"), tags=("close", "close_icon"))


def _shell_hover(tag, on):
    """Piccolo riscontro al mouse senza rendere la card rumorosa."""
    fill = _css(_lighter(SPK_BG, 0.18)) if on else SPK_BG
    shell_canvas.itemconfig(tag + "_bg", fill=fill)
    if tag == "close":
        # Il tag "close" comprende sia il disco sia la X: coloro solo
        # l'icona, altrimenti diventano dello stesso colore e la X sparisce.
        shell_canvas.itemconfig("close_icon", fill=TXT if on else MUT)


for _tag in ("type", "settings", "close"):
    shell_canvas.tag_bind(_tag, "<Enter>", lambda e, t=_tag: _shell_hover(t, True))
    shell_canvas.tag_bind(_tag, "<Leave>", lambda e, t=_tag: _shell_hover(t, False))


def _real_control(w, h, text, font):
    """Controllo Tk reale sopra la grafica della card.

    Su Windows una zona disegnata in un Canvas con colore-chiave puo' essere
    visibile ma non ricevere il click. Questo piccolo Canvas figlio ha invece
    una propria area input opaca e affidabile.
    """
    cv = tk.Canvas(root, width=w, height=h, bg=PILL_BG,
                   highlightthickness=0, bd=0, cursor="hand2")
    r = h / 2
    body = (
        cv.create_rectangle(r, 0, w - r, h, fill=SPK_BG, outline=""),
        cv.create_oval(0, 0, 2 * r, h, fill=SPK_BG, outline=""),
        cv.create_oval(w - 2 * r, 0, w, h, fill=SPK_BG, outline=""),
    )
    label = cv.create_text(w / 2, h / 2, text=text, fill=TXT, font=font)

    def hover_control(on):
        color = _css(_lighter(SPK_BG, 0.18)) if on else SPK_BG
        for item in body:
            cv.itemconfig(item, fill=color)
        cv.itemconfig(label, fill="#ffffff" if on else TXT)

    cv.bind("<Enter>", lambda e: hover_control(True))
    cv.bind("<Leave>", lambda e: hover_control(False))
    return cv


type_control = _real_control(34, 24, "T", (UI_FAMILY, 11, "bold"))
settings_control = _real_control(34, 24, "•••", (UI_FAMILY, 9, "bold"))
close_control = _real_control(22, 22, "×", (UI_FAMILY, 13, "bold"))


# --- espansione/richiamo della card --------------------------------------------
# Su Windows non esiste l'alpha parziale (colore-chiave): l'animazione e' una
# scia di pannelli arrotondati pre-renderizzati. Il centro della card scivola
# dal CENTRO DEL MICROFONO al centro della card finale: entrambi i lati si
# muovono insieme (espansione totale, non solo verso destra). Il primo
# fotogramma e' quasi un cerchio dietro l'orb e si apre fino alla card;
# alla chiusura si richiude a cerchio dentro il microfono.
SHELL_ANIM_MS = 320
_MIC_CX, _MIC_CY = MIC_X + C // 2, MIC_Y + C // 2   # centro dell'orb
_END_CX, _END_CY = PANEL_W // 2, PANEL_H // 2       # centro della card piena
_MILL_W, _MILL_H = 116, 116   # primo fotogramma: cerchio che copre il quadrato opaco del mic
_GROW_FRAMES: dict = {}
_grow = {"panel": None, "job": None}
_shrink = {"panel": None, "job": None}
_SHELL_CONTENT = ("brand-dot", "brand", "type", "settings", "close")


def _shell_content(show):
    """Nasconde/mostra i testi della card durante il volo: si muove solo la forma."""
    st = "normal" if show else "hidden"
    for tag in _SHELL_CONTENT:
        shell_canvas.itemconfig(tag, state=st)


def _ease(t):
    return 1.0 - (1.0 - t) ** 3      # ease-out: parte decisa, atterra morbida


def _shell_frame(t):
    """Card al tempo t (0..1): 1 = card piena al suo posto.

    I fotogrammi sono PRE-GENERATI in sequenza (forma e posizione gia'
    calcolate): durante il volo si sceglie solo l'indice piu' vicino,
    zero Pillow a runtime -> scorrimento fluido anche con il timer Tk.
    """
    if t >= 1.0 or not _SHELL_SEQ:
        shell_canvas.itemconfig(panel_item, image=_PANEL_IMG)
        shell_canvas.config(width=PANEL_W, height=PANEL_H)
        shell_canvas.place(x=0, y=0)
        return
    idx = max(0, min(len(_SHELL_SEQ) - 1, int(round(t * (len(_SHELL_SEQ) - 1)))))
    photo, w, h, x, y = _SHELL_SEQ[idx]
    shell_canvas.itemconfig(panel_item, image=photo)
    shell_canvas.config(width=w, height=h)
    shell_canvas.place(x=x, y=y)


def _anim_reset():
    """Termina subito le animazioni: card completa, code fermate."""
    for d in (_grow, _shrink):
        if d["job"] is not None:
            try:
                root.after_cancel(d["job"])
            except Exception:
                pass
        d["job"] = d["panel"] = None
    _shell_frame(1.0)
    _shell_content(True)


def _grow_step(seq, t0):
    if _grow["panel"] != seq:
        return                       # e' arrivata una chiusura: fermo tutto
    t = min(1.0, (time.monotonic() - t0) / (SHELL_ANIM_MS / 1000.0))
    if t >= 1.0:
        _grow["panel"] = _grow["job"] = None
        _shell_frame(1.0)
        _shell_content(True)
        # a fine corsa il microfono torna opaco sulla card e i controlli
        # veri compaiono sopra
        canvas.config(bg=PILL_BG)
        canvas.itemconfig(img_item, image=_mic_frame(True))
        type_control.place(x=18, y=94)
        settings_control.place(x=PANEL_W - 52, y=94)
        close_control.place(x=PANEL_W - 39, y=12)
        _place_listen()
        _to_top(type_control)
        _to_top(settings_control)
        _to_top(close_control)
        return
    _shell_frame(_ease(t))
    _grow["job"] = root.after(10, _grow_step, seq, t0)


def _shrink_step(seq, t0):
    if _shrink["panel"] != seq:
        return
    t = min(1.0, (time.monotonic() - t0) / (SHELL_ANIM_MS / 1000.0))
    if t >= 1.0:
        _shrink["panel"] = _shrink["job"] = None
        shell_canvas.place_forget()
        # card via: il microfono torna flottante (trasparente)
        canvas.config(bg=TRANSPARENT)
        canvas.itemconfig(img_item, image=_mic_frame(False))
        return
    _shell_frame(_ease(1.0 - t))
    _shrink["job"] = root.after(10, _shrink_step, seq, t0)


# sequenza di 40 fotogrammi pre-generati (forma + posizione): il volo e'
# solo una lettura di indice, nessun rendering Pillow durante l'animazione
_SHELL_SEQ: list = []
for _i in range(40):
    _t = _ease(_i / 39)
    _w = int(round(_MILL_W + (PANEL_W - _MILL_W) * _t))
    _h = int(round(_MILL_H + (PANEL_H - _MILL_H) * _t))
    _r = int(round(_MILL_H / 2 + (PANEL_R - _MILL_H / 2) * _t))
    _cx = _MIC_CX + (_END_CX - _MIC_CX) * _t
    _cy = _MIC_CY + (_END_CY - _MIC_CY) * _t
    _SHELL_SEQ.append((
        ImageTk.PhotoImage(_key(_rounded_panel(_w, _h, max(1, min(_r, _h // 2)),
                                               PILL_BG, PILL_EDGE))),
        _w, _h, int(round(_cx - _w / 2)), int(round(_cy - _h / 2)),
    ))
# NB: niente _shell_frame(1.0) qui: piazzerebbe la card GIA' ALLO STARTUP
# (place dentro _shell_frame). Al via il widget deve essere COLlassato:
# solo il cerchietto, la card nasce al primo hover.

# --- cerchio microfono ---------------------------------------------------------
canvas = tk.Canvas(root, width=C, height=C, bg=TRANSPARENT, highlightthickness=0, bd=0)
canvas.place(x=MIC_X, y=MIC_Y)

# master grandi, poi ridotti a ogni scala: qualita' alta e avvio rapido
_MASTER_D = int(BD * S_MAX) + 1
_mic_master = {
    "idle": _mic_orb_ss(_MASTER_D * SS, ACCENT, _icon_mic),
    "rec": _mic_orb_ss(_MASTER_D * SS, RED, _icon_mic),
}
_disc_cache, _ring_cache, _photo_cache = {}, {}, {}


def _disc(state, s100):
    k = (state, s100)
    if k not in _disc_cache:
        d = max(8, int(round(BD * s100 / 100)))
        frame = Image.new("RGBA", (C, C), (0, 0, 0, 0))
        frame.alpha_composite(_premul_resize(_mic_master[state], (d, d)),
                              ((C - d) // 2, (C - d) // 2))
        _disc_cache[k] = frame
    return _disc_cache[k]


def _ring(k):
    """Anello della registrazione: si allarga e si assottiglia fino a sparire
    (l'alpha non e' disponibile, quindi la 'dissolvenza' e' lo spessore)."""
    if k not in _ring_cache:
        t = k / PULSE_N
        dia = BD * (1.02 + 0.36 * t) * SS
        wid = 3.4 * (1 - t) ** 1.3 * SS
        img = Image.new("RGBA", (C * SS, C * SS), (0, 0, 0, 0))
        if wid >= 0.7 * SS:
            c0 = C * SS / 2
            ImageDraw.Draw(img).ellipse([c0 - dia / 2, c0 - dia / 2, c0 + dia / 2, c0 + dia / 2],
                                        outline=_lighter(RED, 0.30) + (255,), width=int(wid))
        _ring_cache[k] = img.resize((C, C), Image.LANCZOS)
    return _ring_cache[k]


_halo_cache = {}


def _halo(k):
    """Anello dell'ascolto passivo: respiro lento attorno al cerchio.
    Si allarga restringendosi e torna indietro, in ciclo continuo
    (niente alpha su Windows: la dissolvenza e' lo spessore)."""
    if k not in _halo_cache:
        t = k / HALO_N
        wave = 0.5 - 0.5 * np.cos(2 * np.pi * t)      # 0 -> 1 -> 0, dolce
        dia = BD * (1.06 + 0.07 * wave) * SS
        wid = max(2.0, 3.6 - 1.4 * wave) * SS
        img = Image.new("RGBA", (C * SS, C * SS), (0, 0, 0, 0))
        c0 = C * SS / 2
        ImageDraw.Draw(img).ellipse([c0 - dia / 2, c0 - dia / 2, c0 + dia / 2, c0 + dia / 2],
                                    outline=_lighter(ACCENT, 0.30) + (255,), width=int(wid))
        _halo_cache[k] = img.resize((C, C), Image.LANCZOS)
    return _halo_cache[k]


mic_state = {"color": ACCENT}   # colore corrente del cerchio (viola / rosso)
# vero mentre il ciclo di ascolto passivo gira: serve gia' a _mic_frame/_tick
# per l'anello respirante, quindi definito qui una volta sola per tutti
_passive = {"on": False, "mic": None}


def _mic_frame(on_panel=None):
    if on_panel is None:
        on_panel = shell_canvas.winfo_ismapped()
    rec = mic_state["color"] == RED
    passive = _passive["on"] and not rec
    s100 = int(round(min(max(anim["s"], S_MIN), S_MAX) * 100))
    k = int(anim["phase"] * PULSE_N) % PULSE_N if rec else -1
    hk = int(anim["pphase"] * HALO_N) % HALO_N if passive else -1
    key = ("rec" if rec else "idle", s100, k if rec else hk, bool(on_panel))
    ph = _photo_cache.get(key)
    if ph is None:
        if len(_photo_cache) > 400:
            _photo_cache.clear()
        frame = _disc(key[0], s100)
        if rec:
            frame = Image.alpha_composite(_ring(k), frame)
        elif passive:
            frame = Image.alpha_composite(_halo(hk), frame)
        if on_panel:
            # Sopra la card non uso il colore-chiave: altrimenti Windows
            # ritaglia un buco rettangolare e mostra il desktop sottostante.
            bg = Image.new("RGBA", (C, C), _hex(PILL_BG) + (255,))
            frame = Image.alpha_composite(bg, frame)
            rendered = frame.convert("RGB")
        else:
            rendered = _key(frame)
        ph = _photo_cache[key] = ImageTk.PhotoImage(rendered)
    return ph
anim = {"s": 1.0, "v": 0.0, "target": 1.0, "phase": 0.0, "pphase": 0.0, "job": None}
mic_hover = {"on": False}




def _tick():
    a = anim
    # molla sotto-smorzata: gonfia con un piccolo rimbalzo, come una bollicina
    a["v"] = (a["v"] + (a["target"] - a["s"]) * 0.20) * 0.70
    a["s"] += a["v"]
    rec = mic_state["color"] == RED
    if rec:
        a["phase"] = (a["phase"] + 16 / PULSE_MS) % 1.0
    elif _passive["on"]:
        a["pphase"] = (a["pphase"] + 16 / HALO_MS) % 1.0
    settled = abs(a["v"]) < 0.0008 and abs(a["target"] - a["s"]) < 0.003
    if settled and _passive["on"]:
        settled = False  # con l'anello passivo attivo l'animazione non si ferma mai
    if settled:
        a["s"], a["v"] = a["target"], 0.0
    canvas.itemconfig(img_item, image=_mic_frame())
    if settled and not rec:
        a["job"] = None
        return
    a["job"] = root.after(16, _tick)


def _animate(target=None):
    if target is not None:
        anim["target"] = target
    if anim["job"] is None:
        anim["job"] = root.after(16, _tick)


# pre-genera i fotogrammi dell'hover (cosi' il primo passaggio e' gia' fluido)
for _s in range(int(S_MIN * 100), int(S_MAX * 100) + 1):
    _photo_cache[("idle", _s, -1, False)] = ImageTk.PhotoImage(_key(_disc("idle", _s)))

img_item = canvas.create_image(C // 2, C // 2, anchor="center", image=_mic_frame())


def set_mic_color(color):
    mic_state["color"] = color
    if color == RED:
        anim["phase"] = 0.0
    canvas.itemconfig(img_item, image=_mic_frame())
    _animate()


# --- pillola con textbox + tasto invia -----------------------------------------
# Un unico canvas: pillola e tasto invia sono la STESSA immagine, cosi' gli
# angoli del tasto non "bucano" la pillola mostrando il desktop sotto.
# La pillola vive SEMPRE sopra la card: i suoi angoli trasparenti non devono
# bucare fino al desktop (i "triangolini piu' scuri" sui bordi) -> il colore-
# chiave viene riempito col colore della card, identico a PILL_BG, cosi' la
# cucitura con la card e' invisibile.
entry_frame = tk.Canvas(root, width=ENTRY_W, height=ENTRY_H, bg=PILL_BG,
                        highlightthickness=0, bd=0, cursor="xterm")
_SEND_SIZES = [SEND_D, SEND_D + 1, SEND_D + 2, SEND_D_HOVER]
_PILLS = [ImageTk.PhotoImage(_key(_rounded_panel(ENTRY_W, ENTRY_H, ENTRY_H // 2, PILL_BG,
                                                 PILL_EDGE, send_d=sd), base=PILL_BG))
          for sd in _SEND_SIZES]
_pill_item = entry_frame.create_image(0, 0, anchor="nw", image=_PILLS[0])

entry = tk.Entry(entry_frame, bg=PILL_BG, fg=TXT, insertbackground=TXT, relief="flat",
                 font=FONT_UI, highlightthickness=0, bd=0,
                 selectbackground=ACCENT, selectforeground="white")
entry_frame.create_window(14, ENTRY_H // 2, anchor="w", window=entry,
                          width=ENTRY_W - 14 - ENTRY_H - 4, height=20)

send_anim = {"i": 0, "target": 0, "job": None}


def _send_step():
    s = send_anim
    s["i"] += 1 if s["target"] > s["i"] else -1
    entry_frame.itemconfig(_pill_item, image=_PILLS[s["i"]])
    s["job"] = root.after(16, _send_step) if s["i"] != s["target"] else None


def _send_hover(on):
    send_anim["target"] = len(_PILLS) - 1 if on else 0
    entry_frame.config(cursor="hand2" if on else "xterm")
    if send_anim["job"] is None and send_anim["i"] != send_anim["target"]:
        _send_step()


def _in_send(x, y):
    cx, cy = ENTRY_W - ENTRY_H / 2, ENTRY_H / 2
    return (x - cx) ** 2 + (y - cy) ** 2 <= (SEND_D_HOVER / 2 + 1) ** 2


def _pill_click(e):
    if _in_send(e.x, e.y):
        send_text_cmd()
    else:
        entry.focus_set()


entry_frame.bind("<Motion>", lambda e: _send_hover(_in_send(e.x, e.y)))
entry_frame.bind("<Button-1>", _pill_click)

# placeholder (tk.Entry non ce l'ha)
PLACEHOLDER = "Scrivi a Ugo…"  # tradotto da _ph_show via W('placeholder')
ph = {"on": False}
_NAV_KEYS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Left", "Right",
             "Up", "Down", "Home", "End", "Tab", "Escape", "Return", "Caps_Lock", "Win_L", "Win_R"}


def _ph_show():
    if not entry.get():
        ph["on"] = True
        entry.config(fg=MUT)
        entry.insert(0, W("placeholder"))
        entry.icursor(0)


def _ph_clear():
    if ph["on"]:
        ph["on"] = False
        entry.delete(0, "end")
        entry.config(fg=TXT)


def entry_text() -> str:
    return "" if ph["on"] else entry.get().strip()


def _ph_key(e):
    if ph["on"] and e.keysym not in _NAV_KEYS:
        _ph_clear()


def _ph_keyup(_e=None):
    if not ph["on"] and not entry.get():
        _ph_show()


entry.bind("<Key>", _ph_key)
entry.bind("<KeyRelease>", _ph_keyup)
entry.bind("<Button-1>", lambda e: entry.icursor(0) if ph["on"] else None, add="+")

# --- toggle mute del TTS (visibile solo in mouse-over) --------------------------
tts_muted = {"on": bool(prefs.get("muted"))}
_SPK = {
    (False, False): ImageTk.PhotoImage(_key(_glossy(SPK_D, SPK_BG, _icon_speaker(False)))),
    (False, True): ImageTk.PhotoImage(_key(_glossy(SPK_D, _lighter(SPK_BG, 0.12), _icon_speaker(False)))),
    (True, False): ImageTk.PhotoImage(_key(_glossy(SPK_D, RED, _icon_speaker(True)))),
    (True, True): ImageTk.PhotoImage(_key(_glossy(SPK_D, _lighter(RED, 0.12), _icon_speaker(True)))),
}
spk_hover = {"on": False}
mute_btn = tk.Label(root, bd=0, bg=TRANSPARENT, cursor="hand2")


# --- toggle ascolto passivo (wake word), visibile in mouse-over ----------------
listen_disabled = {"on": not bool(prefs.get("listen", True))}  # default: attivo
_LSN = {
    # base=PILL_BG: il tasto vive sempre sulla card, gli angoli trasparenti
    # non devono bucare fino al desktop (vedi pillola della textbox)
    (False, False): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, ACCENT, _icon_mic_on), base=_hex(PILL_BG))),
    (False, True): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, _lighter(ACCENT, 0.12), _icon_mic_on), base=_hex(PILL_BG))),
    (True, False): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, SPK_BG, _icon_mic_off), base=_hex(PILL_BG))),
    (True, True): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, _lighter(SPK_BG, 0.12), _icon_mic_off), base=_hex(PILL_BG))),
}
listen_hover = {"on": False}
listen_btn = tk.Label(root, bd=0, bg=TRANSPARENT, cursor="hand2")


def _listen_refresh():
    listen_btn.config(image=_LSN[(listen_disabled["on"], listen_hover["on"])])


_listen_refresh()

def _spk_refresh():
    mute_btn.config(image=_SPK[(tts_muted["on"], spk_hover["on"])])


_spk_refresh()


# --- card / modalità testo -----------------------------------------------------
def _to_top(w):
    """Widget in cima allo stacking, col comando Tcl nativo.

    lift()/lower() dei widget Canvas sono in realta' tag_raise/tag_lower e
    SENZA argomenti sollevano TclError ("wrong # args"): l'eccezione muore
    nel callback Tk e la card resta mezza costruita (la T non apre la
    textbox e l'orologio di chiusura al mouse-leave non parte mai).
    """
    w.tk.call("raise", w._w)


def open_shell(animate=True):
    """Mostra la card al mouse-over, senza rubare il focus al desktop.

    Con animate=True e card chiusa la card si espande da dietro il
    microfono (i controlli veri compaiono solo a fine corsa); negli altri
    casi la card e' subito completa.
    """
    _wlog(f"open_shell animate={animate} mapped={shell_canvas.winfo_ismapped()}")
    animating = (animate and not shell_canvas.winfo_ismapped()
                 and _grow["job"] is None and _shrink["job"] is None)
    if not animating:
        _anim_reset()
    if not shell_canvas.winfo_ismapped():
        shell_canvas.place(x=0, y=0)
    shell_canvas.tk.call("lower", shell_canvas._w)   # card sotto a tutto
    if not entry_frame.winfo_ismapped():
        canvas.place(x=MIC_X, y=MIC_Y)
        _to_top(canvas)
    if animating:
        # apertura animata: il microfono resta OPACO (bg = colore card).
        # NON usare il colore-chiave qui: su Windows i pixel chiave bucano
        # fino al DESKTOP (non al widget fratello sotto) e durante
        # l'animazione si vedrebbe il rettangolo scuro attorno all'orb.
        # La card copre sempre il quadrato del mic (minimo 166x121), quindi
        # lo square opaco si cuce invisibile alla card che cresce.
        canvas.config(bg=PILL_BG)
        canvas.itemconfig(img_item, image=_mic_frame(True))
        type_control.place_forget()
        settings_control.place_forget()
        close_control.place_forget()
        listen_btn.place_forget()
        _grow["panel"] = (_grow["panel"] or 0) + 1
        _shell_frame(0.0)
        _shell_content(False)   # in volo solo la forma, niente testi
        _grow["job"] = root.after(16, _grow_step, _grow["panel"], time.monotonic())
    else:
        # Versione opaca del frame: nessun buco trasparente dentro la card.
        canvas.config(bg=PILL_BG)
        canvas.itemconfig(img_item, image=_mic_frame(True))
        close_control.place(x=PANEL_W - 39, y=12)
        _to_top(close_control)
        if not entry_frame.winfo_ismapped():
            type_control.place(x=18, y=94)
            settings_control.place(x=PANEL_W - 52, y=94)
            _place_listen()
            _to_top(type_control)
            _to_top(settings_control)
        _shell_frame(1.0)
    _start_hover_watch()


def close_shell():
    """Richiude la card e torna al solo microfono flottante.

    Dalla sola card la chiusura e' animata: la card si richiude DENTRO il
    microfono, che resta sopra e continua a vedersi. Con la textbox aperta
    o un'animazione in corso la chiusura e' immediata.
    """
    _wlog("close_shell")
    listen_btn.place_forget()
    if hover.get("job") is not None:
        try:
            root.after_cancel(hover["job"])
        except Exception:
            pass
        hover["job"] = None
    hover["left_at"] = None
    was_entry = entry_frame.winfo_ismapped()
    close_entry(restore_mic=False)
    type_control.place_forget()
    settings_control.place_forget()
    close_control.place_forget()
    canvas.place(x=MIC_X, y=MIC_Y)
    _to_top(canvas)
    shell_canvas.tk.call("lower", shell_canvas._w)
    # microfono OPACO durante lo shrink (vedi open_shell: i pixel chiave
    # bucherebbero fino al desktop mostrando il rettangolo scuro)
    canvas.config(bg=PILL_BG)
    canvas.itemconfig(img_item, image=_mic_frame(True))
    if was_entry or _grow["panel"] is not None or _shrink["panel"] is not None:
        _anim_reset()
        shell_canvas.place_forget()
        # card via: ora il mic torna flottante trasparente
        canvas.config(bg=TRANSPARENT)
        canvas.itemconfig(img_item, image=_mic_frame(False))
        return
    _shrink["panel"] = (_shrink["panel"] or 0) + 1
    _shell_content(False)       # in volo solo la forma, niente testi
    _shrink["job"] = root.after(16, _shrink_step, _shrink["panel"], time.monotonic())


def open_entry():
    """La T trasforma la card in una piccola modalità di scrittura."""
    _wlog("open_entry (click sulla T)")
    open_shell(animate=False)
    canvas.place_forget()
    type_control.place_forget()
    settings_control.place_forget()
    listen_btn.place_forget()
    shell_canvas.itemconfig("type", state="hidden")
    shell_canvas.itemconfig("settings", state="hidden")
    entry_frame.place(x=PILL_X, y=PILL_Y)
    _to_top(entry_frame)
    _ph_show()
    root.focus_force()
    entry.focus_set()


def close_entry(restore_mic=True):
    _wlog(f"close_entry restore_mic={restore_mic}")
    entry_frame.place_forget()
    entry.delete(0, "end")
    ph["on"] = False
    entry.config(fg=TXT)
    _send_hover(False)
    shell_canvas.itemconfig("type", state="normal")
    shell_canvas.itemconfig("settings", state="normal")
    if restore_mic and shell_canvas.winfo_ismapped():
        _anim_reset()   # niente animazioni a metà quando la textbox si chiude
        canvas.place(x=MIC_X, y=MIC_Y)
        _to_top(canvas)
        type_control.place(x=18, y=94)
        settings_control.place(x=PANEL_W - 52, y=94)
        _place_listen()
        _to_top(type_control)
        _to_top(settings_control)


def toggle_entry():
    if entry_frame.winfo_ismapped():
        close_entry()
    else:
        open_entry()


# La card appare al mouse-over e svanisce poco dopo l'uscita. La T e' l'unico
# punto che apre l'input: cosi' non sembra una barra di chat appiccicata al PC.
hover = {"on": False, "left_at": None, "job": None}


def _pointer_inside_widget():
    """Usa lo stesso sistema di coordinate della finestra.

    Con scaling 125/150% Tk puo' restituire puntatore e geometria in scale
    diverse. Le API Win32 lavorano entrambe in pixel fisici e non sbagliano il
    test; sugli altri sistemi uso il widget realmente sotto al puntatore.
    """
    if pu.IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            point = wintypes.POINT()
            rect = wintypes.RECT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
            ctypes.windll.user32.GetWindowRect(root.winfo_id(), ctypes.byref(rect))
            return rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom
        except Exception:
            pass
    try:
        px, py = root.winfo_pointerxy()
        pointed = root.winfo_containing(px, py)
        return pointed is not None and (
            pointed == root or str(pointed).startswith(str(root) + ".")
        )
    except Exception:
        return False


def _hover_watch():
    """Chiude la card dopo 2 s fuori dai suoi limiti reali.

    Su Windows i pixel a colore-chiave possono non generare sempre gli eventi
    Leave di Tk. Leggere il puntatore rende il comportamento deterministico.
    """
    hover["job"] = None
    if not shell_canvas.winfo_ismapped():
        return
    inside = _pointer_inside_widget()

    if inside:
        hover["on"] = True
        hover["left_at"] = None
        if not hover.get("out_logged"):
            hover["out_logged"] = True
            _wlog("watch: mouse dentro la finestra")
        if _shrink["panel"] is not None and not entry_frame.winfo_ismapped():
            # il mouse e' tornato mentre la card si richiudeva: la riapro
            # al completo invece di lasciarla a meta' strada
            _on_enter()
            return
    else:
        hover["on"] = False
        if not hover.get("out_logged", False):
            hover["out_logged"] = True
            _wlog("watch: mouse fuori dalla finestra (attesa 2s)")
        if hover["left_at"] is None:
            hover["left_at"] = time.monotonic()
        elif time.monotonic() - hover["left_at"] >= 2.0:
            if entry_frame.winfo_ismapped() and entry_text():
                # c'e' un messaggio non ancora inviato: la card resta aperta,
                # come al focus-out ("con testo dentro resta aperta")
                hover["left_at"] = None
            else:
                close_shell()
                return
    hover["job"] = root.after(100, _hover_watch)


def _start_hover_watch():
    if hover["job"] is None:
        hover["job"] = root.after(100, _hover_watch)


def _on_enter(_e=None):
    hover["on"] = True
    hover["left_at"] = None
    open_shell()


def _on_leave(_e=None):
    hover["on"] = False
    # La verifica temporale viene eseguita da _hover_watch: gli eventi Leave
    # servono solo come indicazione immediata, non come unica fonte di verita'.


for _w in (root, canvas, shell_canvas, entry_frame, entry):
    _w.bind("<Enter>", _on_enter)
    _w.bind("<Leave>", _on_leave)


# effetto bollicina sul cerchio (in aggiunta al comportamento sopra)
def _mic_enter(_e=None):
    mic_hover["on"] = True
    if not drag["down"]:
        _animate(HOVER_SCALE)


def _mic_leave(_e=None):
    mic_hover["on"] = False
    if not drag["down"]:
        _animate(1.0)


canvas.bind("<Enter>", _mic_enter, add="+")
canvas.bind("<Leave>", _mic_leave, add="+")
entry_frame.bind("<Leave>", lambda e: _send_hover(False), add="+")


def _spk_set_hover(on):
    spk_hover["on"] = on
    _spk_refresh()


mute_btn.bind("<Enter>", lambda e: _spk_set_hover(True), add="+")
mute_btn.bind("<Leave>", lambda e: _spk_set_hover(False), add="+")


def toggle_mute(_e=None):
    tts_muted["on"] = not tts_muted["on"]
    _spk_refresh()
    stop_tts()  # se sta parlando, zitta subito
    _save_prefs(muted=tts_muted["on"])
    bubble.show(W("voice_off") if tts_muted["on"] else W("voice_on"))


mute_btn.bind("<Button-1>", toggle_mute)


def toggle_listen(_e=None):
    listen_disabled["on"] = not listen_disabled["on"]
    _listen_refresh()
    _save_prefs(listen=not listen_disabled["on"])
    if listen_disabled["on"]:
        _passive["on"] = False  # ferma DAVVERO il ciclo di ascolto (bug: prima continuava)
        stop_tts()
        bubble.show(W("listen_off"))
    else:
        bubble.show(W("listen_on"))
        threading.Thread(target=_passive_loop, daemon=True).start()


listen_btn.bind("<Button-1>", toggle_listen)


def _place_listen():
    """Il tasto on/off dell'ascolto passivo torna sulla card: centro della
    riga inferiore, tra la T e i puntini delle impostazioni."""
    listen_btn.place(x=PANEL_W // 2 - 13, y=94)
    _to_top(listen_btn)


listen_btn.bind("<Enter>", lambda e: (listen_hover.__setitem__("on", True),
                                      _listen_refresh()), add="+")
listen_btn.bind("<Leave>", lambda e: (listen_hover.__setitem__("on", False),
                                      _listen_refresh()), add="+")


# --- tooltip dei controlli della card -------------------------------------------
# I controlli non hanno etichette: senza tooltip la T, i puntini e la X
# sono un enigma la prima volta che si apre la card. Tooltip leggero:
# Toplevel senza decorazioni che appare dopo 650 ms di hover.
_tip = {"win": None, "job": None}


def _tip_hide(_e=None):
    if _tip["job"] is not None:
        try:
            root.after_cancel(_tip["job"])
        except Exception:
            pass
        _tip["job"] = None
    if _tip["win"] is not None:
        try:
            _tip["win"].destroy()
        except Exception:
            pass
        _tip["win"] = None


def _tip_show(text, x, y):
    _tip_hide()
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    tk.Label(win, text=text, bg=CARD, fg=TXT, font=FONT_MUT, padx=8, pady=3,
             bd=1, relief="solid").pack()
    win.wm_geometry(f"+{x}+{y}")
    _tip["win"] = win


def _tip_sched(text_getter, ev):
    _tip_hide()

    def fire():
        _tip["job"] = None
        try:
            _tip_show(text_getter(), ev.x_root + 10, max(4, ev.y_root - 34))
        except Exception:
            pass
    _tip["job"] = root.after(650, fire)


def _bind_tip(widget, key):
    widget.bind("<Enter>", lambda e: _tip_sched(lambda: W(key), e), add="+")
    widget.bind("<Leave>", _tip_hide, add="+")
    widget.bind("<Button-1>", _tip_hide, add="+")


for _ctl, _tipkey in ((type_control, "tip_type"), (settings_control, "tip_settings"),
                      (close_control, "tip_close"), (listen_btn, "tip_listen")):
    _bind_tip(_ctl, _tipkey)


# --- popup: quale trascrittore STT e' attivo (doppio click sul cerchio) -------
def _fetch_json(path):
    with urllib.request.urlopen(ROOT_URL + path, timeout=5) as r:
        return json.loads(r.read().decode())


def _set_model(name):
    """Cambia il modello LLM via server e conferma nella bolla."""
    def run():
        try:
            _post_json("/api/model", {"model": name})
            ui(lambda: bubble.show(W("ai_model", m=name.replace("qwen2.5:", "Qwen "))))
        except Exception as exc:
            ui(lambda: bubble.show(W("ai_model_err", e=exc)))
    threading.Thread(target=run, daemon=True).start()


def _mic_pref_key() -> str:
    """Chiave preferenze per l'attuale strumento di acquisizione del widget:
    'passive' (wake word) o 'manual' (registrazione dal cerchio)."""
    return "mic_passive" if _passive["on"] else "mic_manual"


def _apply_mic_choice(name: str) -> None:
    """Salva la scelta microfono nel file preferenze e applica il cambio a
    caldo: il loop passivo si riavvia sul nuovo input (quando termina)."""
    _save_prefs(**{_mic_pref_key(): name})
    if _passive["on"]:
        _passive["on"] = False          # il thread esce entro ~0.1 s
        # il ritardo evita la corsa col finally del vecchio loop, che rimette
        # _passive["on"] = False mentre il nuovo ciclo sta gia' partendo
        ui(lambda: root.after(500, _ensure_passive))
    bubble.show(W("mic_saved", m=name))


# --- popup impostazioni: card custom (il tk.Menu nativo e' piatto e spaiato col widget) ---
_menu_card = {"win": None}


def _menu_card_close(_e=None):
    w = _menu_card["win"]
    if w is not None:
        try:
            w.destroy()
        except Exception:
            pass
        _menu_card["win"] = None


def _fit_text(fnt, text: str, px: int) -> str:
    """Tronca con '…' il testo che sfora la larghezza della card."""
    if fnt.measure(text) <= px:
        return text
    while text and fnt.measure(text + "…") > px:
        text = text[:-1]
    return text + "…"


def _show_menu_card(sections, right_x: int, top_y: int, anchor_right: bool = True):
    """Popup impostazioni come card disegnata: fondo arrotondato (la stessa
    pillola della bolla), righe con icona e hover, spunta a destra, separatori.
    sections = lista di sezioni; ogni sezione = lista di item
    {icon, text, enabled, mark, cmd} oppure {'sep': True}."""
    _menu_card_close()
    ROW_H, SEP_H, SEC_GAP, PADX, PADY = 28, 11, 9, 14, 12
    ICON_W, MARK_W, MIN_W, MAX_W = 24, 26, 252, 340
    HOVER = "#262b45"
    win = tk.Toplevel(root)
    _menu_card["win"] = win
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    if pu.IS_WINDOWS:
        win.attributes("-transparentcolor", TRANSPARENT)
    else:
        win.attributes("-alpha", 0.95)
    win.configure(bg=TRANSPARENT)
    cv = tk.Canvas(win, bg=TRANSPARENT, highlightthickness=0, bd=0)
    cv.pack()

    fnt = tkfont.Font(root=root, font=FONT_UI)
    rows, row_of, y, tw = [], {}, PADY, MIN_W
    for sec in sections:
        for it in sec:
            if it.get("sep"):
                continue
            need = fnt.measure(it.get("text", "")) + ICON_W + MARK_W + 16 + PADX * 2
            tw = max(tw, min(MAX_W, need))
        for it in sec:
            rows.append((it, y))
            y += SEP_H if it.get("sep") else ROW_H
        y += SEC_GAP
    W, H = tw, y - SEC_GAP + PADY
    avail = W - PADX * 2 - ICON_W - MARK_W

    bg = ImageTk.PhotoImage(_key(_rounded_panel(W, H, 16, CARD, PILL_EDGE)))
    win._bg = bg                       # riflesso GC: senza ref l'immagine sparisce
    cv.configure(width=W, height=H)
    cv.create_image(0, 0, image=bg, anchor="nw")

    def _hover(tag, on):
        cv.itemconfigure(tag, fill=HOVER if on else "")
        cv.config(cursor="hand2" if on else "arrow")

    def _click(tag):
        it = row_of[tag]
        if not it.get("enabled") or not it.get("cmd"):
            return
        _menu_card_close()
        root.after(30, it["cmd"])

    for it, ry in rows:
        cy = ry + (ROW_H - 2) / 2
        if it.get("sep"):
            cv.create_line(PADX, cy, W - PADX, cy, fill=PILL_EDGE)
            continue
        tag = f"r{ry}"          # un tag per riga: hover e click su TUTTA la riga
        row_of[tag] = it
        cv.create_rectangle(PADX - 6, ry, W - PADX + 6, ry + ROW_H - 4,
                            fill="", outline="", tags=tag)
        icon = it.get("icon", "")
        if icon:
            cv.create_text(PADX + 2, cy, text=icon, anchor="w",
                           fill=ACCENT if it.get("mark") else MUT, font=FONT_UI,
                           tags=tag)
        cv.create_text(PADX + ICON_W, cy, text=_fit_text(fnt, it.get("text", ""), avail),
                       anchor="w", fill=TXT if it.get("enabled") else MUT, font=FONT_UI,
                       tags=tag)
        if it.get("mark"):
            cv.create_text(W - PADX - MARK_W // 2, cy, text="✓", fill=ACCENT,
                           font=FONT_UI, tags=tag)
        if it.get("enabled"):
            cv.tag_bind(tag, "<Enter>", lambda e, tg=tag: _hover(tg, True))
            cv.tag_bind(tag, "<Leave>", lambda e, tg=tag: _hover(tg, False))
            cv.tag_bind(tag, "<Button-1>", lambda e, tg=tag: _click(tg))

    def _arm():
        try:
            win.focus_force()
        except Exception:
            pass
        win.bind("<Escape>", _menu_card_close)
        win.bind("<FocusOut>", _menu_card_close)

    # il ritardo evita che il focus_force uccida il click che ha aperto il menu
    win.after(120, _arm)
    sx, sy = win.winfo_screenwidth(), win.winfo_screenheight()
    px = (right_x - W) if anchor_right else right_x
    px = min(max(8, px), sx - W - 8)
    py = min(max(8, top_y), sy - H - 8)
    win.wm_geometry(f"+{px}+{py}")


def show_stt_popup():
    """Menu impostazioni: info trascrittore, microfono, voce, modello AI."""
    on_release._n = -1  # annulla un eventuale toggle in attesa dal primo click

    def work():
        stt_text = W("stt_unavail", e="?")
        try:
            info = _fetch_json("/api/stt")
            eng = info.get("engine", "?")
            name = {"whisper": "Whisper", "vosk": "Vosk"}.get(eng, eng)
            stt_text = f"{W('stt')}: {name} · {info.get('model', '?')} · {W('device')}: {info.get('device', '?')}"
        except Exception:
            pass
        try:
            mod = _fetch_json("/api/model")
            current, available = mod.get("active", ""), mod.get("available", [])
        except Exception:
            current, available = "", []
        # micro disponibili: soundcard + riserva PortAudio (driver che soundcard
        # non apre, es. Shure MV6); i nomi doppi compaiono una volta sola
        seen, mics = set(), []
        for src in (lambda: [m.name for m in sc.all_microphones()],
                    lambda: [m.name for m in _sd_all_mics()]):
            try:
                for nm in src():
                    if nm and nm not in seen:
                        seen.add(nm)
                        mics.append(nm)
            except Exception:
                continue
        cur = prefs.get(_mic_pref_key())  # None = scelta automatica (probe)

        def _toggle_and_refresh(fn):
            fn()
            root.after(350, show_stt_popup)   # la card si ridisegna col nuovo stato

        def build():
            mic_items = [{"icon": "🎙️", "text": W("micro"), "enabled": False}]
            if mics:
                if cur:
                    mic_items.append({"text": W("mic_auto"), "enabled": True,
                                      "cmd": _mic_reset_choice})
                else:
                    mic_items.append({"text": W("mic_auto"), "enabled": False,
                                      "mark": True})
                for name in mics:
                    if name == cur:
                        mic_items.append({"text": name, "enabled": False, "mark": True})
                    else:
                        mic_items.append({"text": name, "enabled": True,
                                          "cmd": lambda n=name: _apply_mic_choice(n)})
            else:
                mic_items.append({"text": W("mic_none"), "enabled": False})
            model_items = [{"icon": "✨", "text": W("model") + " AI", "enabled": False}]
            for name in available:
                label = name.replace("qwen2.5:", "Qwen ")
                if name == current:
                    model_items.append({"text": label, "enabled": False, "mark": True})
                else:
                    model_items.append({"text": label, "enabled": True,
                                        "cmd": lambda n=name: _set_model(n)})
            _show_menu_card(
                [[{"icon": "🎤", "text": stt_text, "enabled": False}],
                 [{"icon": "🔇" if tts_muted["on"] else "🔊",
                   "text": W("voice_on") if tts_muted["on"] else W("voice_off"),
                   "enabled": True, "cmd": lambda: _toggle_and_refresh(toggle_mute)},
                  {"icon": "🚫" if listen_disabled["on"] else "🎙️",
                   "text": W("listen_off") if not listen_disabled["on"] else W("listen_on"),
                   "enabled": True, "cmd": lambda: _toggle_and_refresh(toggle_listen)}],
                 mic_items,
                 model_items],
                root.winfo_x() + PANEL_W - 8, root.winfo_y() + 34)
        ui(build)
    threading.Thread(target=work, daemon=True).start()


canvas.bind("<Double-Button-1>", lambda e: show_stt_popup())


def _shell_click(e):
    """Hit area esplicite: il click funziona anche sui bordi delle pillole."""
    _wlog(f"shell_click x={e.x:.0f} y={e.y:.0f}")
    if PANEL_W - 46 <= e.x <= PANEL_W - 10 and 7 <= e.y <= 39:
        _quit()  # chiusura completa: ascolto, registrazione e TTS
    elif 12 <= e.x <= 58 and 88 <= e.y <= 124:
        toggle_entry()
    elif PANEL_W - 58 <= e.x <= PANEL_W - 12 and 88 <= e.y <= 124:
        show_stt_popup()
    return "break"


shell_canvas.bind("<Button-1>", _shell_click)
type_control.bind("<Button-1>", lambda e: toggle_entry())
settings_control.bind("<Button-1>", lambda e: show_stt_popup())
close_control.bind("<Button-1>", lambda e: _quit())
for _control in (type_control, settings_control, close_control):
    _control.bind("<Enter>", _on_enter, add="+")
    _control.bind("<Leave>", _on_leave, add="+")


# --- rete ---------------------------------------------------------------------
def _post_json(path, payload):
    req = urllib.request.Request(ROOT_URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def _post_wav(wav: bytes, wake: int = 0):
    req = urllib.request.Request(ROOT_URL + f"/api/listen_wav?wake={wake}", data=wav,
                                 headers={"Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def _meta_of(e):
    if e.get("intent") and e["intent"] != "-":
        return f"{e['intent']} · {e.get('detector', '')} · {e.get('ms', 0)} ms"
    return ""


# ---------------------------------------------------------------------------
# i18n widget: la lingua vive nel server (bandiera web UI, /api/lang); il
# widget la segue con un polling leggero e traduce le sue stringhe.
# ---------------------------------------------------------------------------
def _fetch_lang() -> str:
    try:
        with urllib.request.urlopen(ROOT_URL + "/api/lang", timeout=3) as r:
            return (json.loads(r.read().decode()).get("lang") or "it").lower()[:2]
    except Exception:
        return "it"


_WSTR = {
    "it": {
        "placeholder": "Scrivi a Ugo…",
        "listen_click": "\U0001F3A4 Sto ascoltando... clicca di nuovo per inviare",
        "transcribing": "Trascrivo…",
        "understand": "Capisco…",
        "i_listen": "\U0001F3A4 Ti ascolto…",
        "nothing": "Non ho sentito niente.",
        "nothing_rec": "Non ho registrato nulla.",
        "mic_error": "Errore microfono: {e}",
        "server_error": "Errore server: {e}",
        "error": "Errore: {e}",
        "starting": "Avvio del server…",
        "ready": "Pronto.",
        "srv_down": "Server non raggiungibile.",
        "voice_off": "Voce disattivata.", "voice_on": "Voce riattivata.",
        "listen_off": "Ascolto passivo disattivato.",
        "listen_on": "Ascolto passivo attivo: dimmi 'Ugo'.",
        "ai_model": "Modello AI: {m}", "ai_model_err": "Errore modello: {e}",
        "micro": "Microfono", "mic_auto": "Automatico (sceglie Ugo)",
        "mic_saved": "Microfono: {m}", "mic_none": "Nessun microfono trovato.",
        "vosk_missing": "Modello Vosk mancante: ascolto passivo non disponibile.",
        "stt": "Trascrittore", "model": "Modello", "device": "Dispositivo",
        "stt_unavail": "Trascrittore non disponibile ({e})",
        "tip_type": "Scrivi a Ugo", "tip_settings": "Impostazioni",
        "tip_close": "Chiudi Ugo", "tip_listen": "Ascolto passivo on/off",
    },
    "en": {
        "placeholder": "Type to Ugo…",
        "listen_click": "\U0001F3A4 Listening... click again to send",
        "transcribing": "Transcribing…",
        "understand": "Got it…",
        "i_listen": "\U0001F3A4 I'm listening…",
        "nothing": "I didn't hear anything.",
        "nothing_rec": "I didn't record anything.",
        "mic_error": "Microphone error: {e}",
        "server_error": "Server error: {e}",
        "error": "Error: {e}",
        "starting": "Starting the server…",
        "ready": "Ready.",
        "srv_down": "Server unreachable.",
        "voice_off": "Voice off.", "voice_on": "Voice on.",
        "listen_off": "Passive listening off.",
        "listen_on": "Passive listening on: say 'Ugo'.",
        "ai_model": "AI model: {m}", "ai_model_err": "Model error: {e}",
        "micro": "Microphone", "mic_auto": "Automatic (Ugo picks)",
        "mic_saved": "Microphone: {m}", "mic_none": "No microphone found.",
        "vosk_missing": "Vosk model missing: passive listening unavailable.",
        "stt": "Transcriber", "model": "Model", "device": "Device",
        "stt_unavail": "Transcriber unavailable ({e})",
        "tip_type": "Type to Ugo", "tip_settings": "Settings",
        "tip_close": "Close Ugo", "tip_listen": "Passive listening on/off",
    },
}


def W(key: str, **kw) -> str:
    lang = _lang_state["lang"] if _lang_state["lang"] in _WSTR else "it"
    s = _WSTR[lang].get(key) or _WSTR["it"][key]
    return s.format(**kw) if kw else s


_lang_state = {"lang": "it"}


def _lang_poller():
    """Polling leggero della lingua: aggiorna placeholder e bolla se cambia."""
    while True:
        time.sleep(4)
        nl = _fetch_lang()
        if nl != _lang_state["lang"]:
            _lang_state["lang"] = nl
            def upd():
                try:
                    entry.config(font=FONT_UI)  # no-op per toccare il main thread
                    _ph_clear()
                    _ph_show()
                except Exception:
                    pass
            ui(upd)


threading.Thread(target=_lang_poller, daemon=True).start()


def stop_tts():
    """Interrompe subito l'eventuale riproduzione vocale in corso."""
    pu.tts_stop()


def _show_entry(e):
    bubble.show(e.get("assistant") or e.get("error") or "errore", raw=e.get("raw"))
    if tts_muted["on"]:
        return  # muto: la risposta resta solo scritta nella bolla
    pu.tts_play_file(BASE / "_tts_reply.wav")


def send_text_cmd():
    text = entry_text()
    if not text:
        return
    close_entry()
    stop_tts()  # nuovo input: interrompi la voce che sta parlando
    bubble.show("…", sticky=True)

    def run():
        try:
            res = _post_json("/api/text", {"text": text})
            ui(lambda: _show_entry(res))
        except Exception as exc:
            msg = W("error", e=exc)  # 'exc' non esiste piu' fuori dall'except
            ui(lambda: bubble.show(msg))

    threading.Thread(target=run, daemon=True).start()


entry.bind("<Return>", lambda e: send_text_cmd())
entry.bind("<Escape>", lambda e: close_entry())


def _on_focus_out(_e=None):
    # la textbox si chiude da sola quando il focus va a un'altra app.
    # ATTENZIONE: focus_force + focus_set di open_entry generano un
    # <FocusOut> su root ANCHE quando il focus resta dentro Ugo (root ->
    # entry): chiudere in quel caso fa aprire/chiudere la textbox nello
    # stesso istante ("la T non funziona"). Chiudiamo SOLO quando e'
    # CERTO che il primo piano e' di un'altra applicazione.
    if not entry_frame.winfo_ismapped():
        return
    if pu.IS_WINDOWS:
        try:
            import ctypes
            fg = ctypes.windll.user32.GetForegroundWindow()
            if fg:  # c'e' una finestra in primo piano
                mine = int(root.wm_frame(), 16)
                if fg == mine:
                    return  # siamo noi in primo piano: focus nostro
        except Exception:
            _wlog("focus_out: fg non leggibile, non chiudo")
            return  # in dubbio NON chiudere: il mouse-out resta la rete
    else:
        try:
            if root.focus_get() is not None:
                return
        except Exception:
            return  # focus non determinabile: non chiudere
    if not entry_text():  # con testo dentro resta aperta
        _wlog("focus_out: chiudo (focus a altra app)")
        close_entry()


root.bind("<FocusOut>", _on_focus_out)

# --- registrazione microfono ---------------------------------------------------
rec_flag = threading.Event()


def _err_text(exc: Exception) -> str:
    """Messaggio d'errore leggibile: alcuni errori della libreria audio
    (assert interni, errori COM) arrivano come str() VUOTA e nei log
    compariva solo 'ERRORE:' senza la minima diagnosi."""
    s = str(exc).strip()
    return s or exc.__class__.__name__


class _SDMic:
    """Microfono di riserva via sounddevice (PortAudio): stesso minuscolo
    protocollo degli oggetti soundcard (.name, .id, .recorder()). Serve per i
    driver che soundcard non apre, es. lo Shure MV6 il cui mix format non e'
    estensibile e fa fallire un assert interno della libreria."""

    def __init__(self, name: str, index: int):
        self.name, self.id = name, f"sd:{index}"
        self._index = index

    def recorder(self, samplerate=SR, channels=None, blocksize=None):
        import sounddevice as sd
        outer = self

        class _Rec:
            def __enter__(self):
                self.stream = sd.InputStream(samplerate=samplerate, channels=1,
                                             dtype="float32", device=outer._index)
                self.stream.start()
                return self

            def __exit__(self, *exc):
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                return False

            def record(self, numframes):
                data, _ov = self.stream.read(numframes)
                return data.copy()

        return _Rec()


def _is_trunc(a: str, b: str) -> bool:
    """Vero se un nome e' la versione troncata dell'altro (PortAudio/MME
    taglia a 32 caratteri: 'Analogue 1 + 2 (Focusrite USB A')."""
    m = min(len(a), len(b))
    return m >= 16 and a[:m] == b[:m]


def _sd_all_mics() -> list:
    """Input di sounddevice deduplicati (PortAudio espone lo stesso device su
    piu' host API: MME, DirectSound, WASAPI, WDM-KS; i nomi MME sono troncati).
    Vince sempre la variante col nome piu' lungo."""
    try:
        import sounddevice as sd
        raw = [_SDMic(d.get("name") or "", d["index"])
               for d in sd.query_devices() if d.get("max_input_channels", 0) > 0
               and d.get("name")]
        out = []
        for m in sorted(raw, key=lambda x: -len(x.name)):
            if not any(_is_trunc(m.name, k.name) for k in out):
                out.append(m)
        return out
    except Exception:
        return []


def _sd_mic_by_name(name: str):
    """Cerca un input per nome tra i device sounddevice; i nomi MME sono
    troncati a 32 caratteri, quindi la seconda passata matcha 'contiene'."""
    for d in _sd_all_mics():
        if d.name == name:
            return d
    for d in _sd_all_mics():
        if name in d.name or d.name in name:
            return d
    return None


def _pick_mic(for_passive: bool = False):
    """Sceglie il microfono per lo strumento richiesto.

    `sc.default_microphone()` segue il dispositivo predefinito di Windows, che
    puo' essere un input di linea silenzioso (es. interfaccia audio senza
    nulla collegato). Prima vale la scelta manuale dal menu impostazioni
    (prefs['mic_passive'] per l'ascolto passivo, prefs['mic_manual'] per la
    registrazione dal cerchio; prefs['mic'] e' il legacy pre-menu, vale per
    entrambi); se il nome salvato non esiste piu' (es. micro scollegato) si
    riparte dal probe. Se soundcard non riesce ad aprire il device (driver con
    mix format non gestito, es. Shure MV6) si passa al backend di riserva
    sounddevice/PortAudio, che espone la stessa interfaccia minima. Fallback:
    il microfono predefinito.
    """
    saved = prefs.get("mic_passive" if for_passive else "mic_manual") or prefs.get("mic")
    if saved:
        for m in sc.all_microphones():
            if m.name == saved:
                try:  # verifica d'apertura: alcuni driver passano l'elenco ma
                    with m.recorder(samplerate=SR) as r:   # non il recorder
                        r.record(numframes=SR // 100)
                    return m
                except Exception:
                    break
        m2 = _sd_mic_by_name(saved)
        if m2 is not None:
            _plog(f"soundcard non apre {saved!r}: uso sounddevice (PortAudio)")
            return m2
    # probe automatico: vince l'input piu' vivo; sotto la soglia e' un input
    # morto (collegato ma muto). Il risultato si ricorda solo se non esiste
    # gia' una scelta esplicita (del menu o legacy): non sovrascriverla.
    try:
        best, best_rms = None, 0.0
        sc_names = {m.name for m in sc.all_microphones()}
        sd_skip = lambda nm: nm in sc_names or any(_is_trunc(nm, s) for s in sc_names)
        for m in sc.all_microphones():
            try:
                peak = 0.0
                with m.recorder(samplerate=SR) as r:
                    for _ in range(6):  # ~0.6 s di rumore di fondo
                        a = r.record(numframes=SR // 10).copy()
                        peak = max(peak, float(np.sqrt(np.mean(a ** 2))))
            except Exception:
                continue
            if peak > best_rms:
                best, best_rms = m, peak
        for m in _sd_all_mics():  # driver che soundcard rifiuta (es. MV6)
            if sd_skip(m.name):
                continue
            try:
                peak = 0.0
                with m.recorder(samplerate=SR) as r:
                    for _ in range(6):
                        a = r.record(numframes=SR // 10).copy()
                        peak = max(peak, float(np.sqrt(np.mean(a ** 2))))
            except Exception:
                continue
            if peak > best_rms:
                best, best_rms = m, peak
        if best is not None and best_rms > 0.002:  # sotto: input morto/silenzioso
            if not saved:
                _save_prefs(mic=best.name)
            return best
    except Exception:
        pass
    try:
        return sc.default_microphone()
    except Exception:            # nemmeno il default si apre: riserva PortAudio
        sd_mics = _sd_all_mics()
        return sd_mics[0] if sd_mics else None


def _rec_thread():
    chunks = []
    try:
        mic = _pick_mic()
        with mic.recorder(samplerate=SR) as rec:
            while rec_flag.is_set():
                chunks.append(rec.record(numframes=SR // 10).copy())
    except Exception as exc:
        rec_flag.clear()
        msg = W("mic_error", e=_err_text(exc))
        ui(lambda: (set_mic_color(ACCENT), bubble.show(msg)))
        return
    audio = np.concatenate(chunks) if chunks else np.zeros((0, 1), np.float32)
    ui(lambda: set_mic_color(ACCENT))
    if audio.size == 0:
        ui(lambda: bubble.show(W("nothing_rec")))
        return
    pcm = (np.clip(audio[:, 0], -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    ui(lambda: bubble.show(W("transcribing"), sticky=True))
    try:
        res = _post_wav(buf.getvalue())
        ui(lambda: _show_entry(res))
    except Exception as exc:
        msg = W("server_error", e=exc)
        ui(lambda: bubble.show(msg))


# --- ascolto passivo (wake word "chicco") con Vosk in streaming ---------------
# Vosk e' leggero (il modello e' gia' in RAM per il fallback STT): ascolta in
# continuo, e quando sente 'chicco' (o varianti) apre la registrazione vera e
# invia l'audio al server. Consuma quasi zero CPU: riconoscitore parziale.
import unicodedata

# grafie tutte normalizzate (minuscolo, senza accenti ne' punteggiatura);
# 'qui quo' e 'kiriko' sono storpiature REALI viste da Vosk
_WAKE_TOK = ("ugo", "ugo'", "hugo", "sugo", "wugo", "yugo", "jugo", "ughigo",
             "ugoo", "uugo", "uhgo", "riugo", "truogo", "fuoco")
_WAKE_FILLERS = ("ehi", "hey", "oh", "ehila", "ciao", "su", "a", "allora")
_WAKE_PUNCT = str.maketrans("", "", "!.,;:?\"'`’")
_PLOG = BASE / "passive_log.txt"


def _plog(msg: str) -> None:
    """Log diagnostico del passivo: cosa sente Vosk, livello, wake, invii."""
    try:
        with _PLOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


def _norm_tok(t: str) -> str:
    """Token normalizzato: minuscolo, senza accenti (chicó/chicò -> chicco)
    e senza punteggiatura finale (chicco! -> chicco)."""
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.translate(_WAKE_PUNCT)


def _wake_hit(txt: str) -> bool:
    """La wake word deve APRIRE la frase (eventuale riempitivo davanti):
    'chicco apri spotify' o 'ehi chicco apri' si', 'un chicco di caffe' no.
    Accetta le grafie alternative e le code fonetiche (chiccoo, chiccoh...)."""
    toks = [_norm_tok(t) for t in txt.split()]
    toks = [t for t in toks if t]
    if toks and toks[0] in _WAKE_FILLERS:
        toks = toks[1:]
    if not toks:
        return False
    first = toks[0]
    if first in _WAKE_TOK:
        return True
    # token che APRE con la wake + 1-2 lettere di coda ('chiccoo', 'chiccoh'):
    # il limite di lunghezza evita falsi positivi tipo 'cicolano'
    if any(first.startswith(w) and len(first) <= len(w) + 2 for w in _WAKE_TOK):
        return True
    return " ".join(toks[:2]) in _WAKE_TOK  # storpiature a due parole ('qui quo')


def _pcm16_of(chunk) -> bytes:
    """float32 (N,1|2) di soundcard -> PCM 16 bit mono (per Vosk e per il WAV)."""
    return (np.clip(chunk[:, 0], -1, 1) * 32767).astype("<i2").tobytes()


def _passive_send_wav(pcm: bytes):
    """Invia l'audio del comando catturato al server e mostra la risposta."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    try:
        res = _post_wav(buf.getvalue(), wake=1)   # il server verifica 'Ugo' con Whisper
        if res.get("silent"):
            # wake-guard: il server non ha sentito la wake word nella frase ->
            # falso positivo del rilevatore economico: taccio tutto e svanisco
            _plog(f"guard: falso positivo scartato ({(res.get('user') or '')!r})")
            ui(lambda: bubble.hide())
            return
        ui(lambda: _show_entry(res))
        _plog(f"risposta: {(res.get('assistant') or res.get('error') or '?')!r} "
              f"[{res.get('intent', '-')} / {res.get('detector', '-')} / "
              f"{res.get('ms', 0)} ms]")
    except Exception as exc:
        msg = W("server_error", e=exc)
        ui(lambda: bubble.show(msg))
        _plog(f"ERRORE server: {exc}")


def _ensure_mic_volume(mic_dev) -> None:
    """Porta il volume di registrazione Windows del microfono USATO a >= 90%:
    matcha il dispositivo per ID endpoint (soundcard e MMDevice condividono
    lo stesso ID) per non regolarmi un input diverso da quello in uso."""
    if not pu.IS_WINDOWS:
        return
    if isinstance(mic_dev, _SDMic):   # device PortAudio: nessun endpoint Windows
        return
    try:
        import comtypes
        from comtypes import CLSCTX_ALL, CoCreateInstance, GUID
        from pycaw.constants import EDataFlow, DEVICE_STATE
        from pycaw.pycaw import IMMDeviceEnumerator, IAudioEndpointVolume
        comtypes.CoInitialize()
        en = CoCreateInstance(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
                              IMMDeviceEnumerator, CLSCTX_ALL)
        coll = en.EnumAudioEndpoints(EDataFlow.eCapture.value,
                                     DEVICE_STATE.ACTIVE.value)
        want = getattr(mic_dev, "id", "") or ""
        for i in range(coll.GetCount()):
            dev = coll.Item(i)
            did = dev.GetId()
            if want and not (did == want or did.endswith(want) or want.endswith(did)):
                continue
            ep = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None
                              ).QueryInterface(IAudioEndpointVolume)
            cur = ep.GetMasterVolumeLevelScalar()
            if cur < 0.9:
                ep.SetMasterVolumeLevelScalar(1.0, None)
                _plog(f"volume microfono: {cur * 100:.0f}% -> 100% "
                      f"({getattr(mic_dev, 'name', '?')})")
            else:
                _plog(f"volume microfono: {cur * 100:.0f}% "
                      f"({getattr(mic_dev, 'name', '?')})")
            break
    except Exception as exc:
        _plog(f"volume microfono non regolabile: {exc}")


def _passive_loop():
    """Ascolto passivo con wake word 'chicco': Vosk in streaming sul microfono,
    quasi zero CPU; alla wake word registra il comando e lo manda al server.
    Si mette in pausa durante la registrazione manuale (cerchio) e riparte dopo."""
    if _passive["on"]:
        return
    _passive["on"] = True
    ui(lambda: _animate())  # avvia l'anello respirante
    try:
        from vosk import KaldiRecognizer, Model as VoskModel
        vosk_dir = Path.home() / ".cache" / "vosk" / "vosk-model-small-it-0.22"
        if not vosk_dir.is_dir():
            ui(lambda: bubble.show(W("vosk_missing")))
            return
        model = VoskModel(str(vosk_dir))
        mic_dev = _pick_mic(for_passive=True)
        _plog(f"avvio: mic={getattr(mic_dev, 'name', '?')} modello={vosk_dir.name}")
        _ensure_mic_volume(mic_dev)

        def rtxt(r, meth):
            try:
                key = "text" if meth == "FinalResult" else "partial"
                return json.loads(getattr(r, meth)()).get(key, "")
            except Exception:
                return ""

        rec = KaldiRecognizer(model, SR)
        chunks = []          # coda d'anello: ultimi ~3 s prima della wake word
        armed = False
        since_voice = 0.0
        quiet_until = 0.0    # immunita' all'eco: niente wake subito dopo una risposta
        last_status = 0.0    # per lo stato periodico nel ramo non-armed
        # wake word neurale (openWakeWord): PRIMARIA se il modello custom esiste,
        # Vosk resta fallback per la wake e motore per endpointing/trascrizione
        oww_model, oww_key, oww_thr, oww_hits = None, "", 0.0, 0
        try:
            mpath = BASE / "ww_ugo.onnx"
            meta = {}
            try:
                meta = json.loads((BASE / "ww_ugo.json").read_text())
            except Exception:
                pass
            # si attiva SOLO se il modello custom ha superato la validazione
            # streaming (TPR >= 60% a FPR 0): altrimenti e' un rumore in piu'
            if mpath.exists() and float(meta.get("stream_tpr", 0)) >= 0.6:
                from openwakeword.model import Model as OwwModel
                oww_model = OwwModel(wakeword_models=[str(mpath)],
                                     inference_framework="onnx")
                oww_key = list(oww_model.models.keys())[0]
                oww_thr = float(meta.get("threshold", 0.7))
                _plog(f"OWW attivo: {mpath.name} (soglia {oww_thr:.2f}, "
                      f"TPR {meta.get('stream_tpr')})")
            elif mpath.exists():
                _plog(f"OWW custom non valido (TPR {meta.get('stream_tpr')}): "
                      "uso solo Vosk + guard Whisper")
        except Exception as exc:
            _plog(f"OWW non disponibile ({exc}): solo Vosk")
        VOICE_LEVEL = 60     # sotto: silenzio (fondo ~2-20); la voce e' oltre ~100
        # fine comando: dopo una frase completata secondo Vosk bastano 0.3 s di
        # silenzio; senza segnale esplicito si attende 0.7 s (prima: sempre 1.0 s)
        PRE_WAKE = 12        # chunk (1.2 s) di audio pre-wake inviati al server
        # AGC: se sei lontano dal microfono il segnale e' debole -> guadagno
        # software progressivo (con limitatore) prima di Vosk/Whisper
        MAX_GAIN, SPEECH_TARGET, NOISE_CEIL = 12.0, 2200.0, 400.0
        agc_gain, agc_n = 1.0, 0
        noise, speech = 20.0, 300.0   # stime RMS di fondo e di voce
        with mic_dev.recorder(samplerate=SR) as mic:
            while _passive["on"]:
                if rec_flag.is_set():
                    time.sleep(0.3)  # registrazione manuale attiva: riparto dopo
                    continue
                audio = mic.record(numframes=SR // 10).copy()
                audio = np.clip(audio * agc_gain, -1, 1)   # guadagno AGC
                pcm = _pcm16_of(audio)
                level = float(np.sqrt(np.mean(
                    np.frombuffer(pcm, "<i2").astype(np.float32) ** 2)))
                thr = max(VOICE_LEVEL, noise * 3.0)  # soglia voce adattiva
                if level > thr and level > 80:       # stima del parlato
                    speech = 0.95 * speech + 0.05 * level
                elif level < max(20.0, noise * 1.5): # stima del fondo
                    noise = 0.95 * noise + 0.05 * max(level, 1.0)
                agc_n += 1
                if agc_n >= 10:  # ~1 s: ricalcolo il guadagno target
                    agc_n = 0
                    g = min(max(SPEECH_TARGET / max(speech, 80.0), 1.0), MAX_GAIN)
                    if noise * g > NOISE_CEIL:  # il fondo non deve esplodere
                        g = min(g, NOISE_CEIL / max(noise, 1.0))
                    agc_gain = 0.85 * agc_gain + 0.15 * g
                oww_s = 0.0
                if oww_model is not None:
                    try:
                        oww_s = float(oww_model.predict(
                            audio[:, 0].astype(np.float32)).get(oww_key, 0.0))
                    except Exception:
                        pass
                if armed:
                    sent_done = rec.AcceptWaveform(pcm)  # Vosk: frase completata?
                    chunks.append(pcm)
                    since_voice = 0.0 if level > thr else since_voice + 0.1
                    spoke_s += 0.1 if level > thr else 0.0  # voce REALE dopo la wake
                    dur = sum(len(c) for c in chunks) / 2 / SR
                    if dur >= 8 or (dur > 0.6 and since_voice > (0.3 if sent_done else 0.7)):
                        if spoke_s < 0.25:
                            # solo la wake word (o rumore): niente comando detto ->
                            # non inviare nulla: Whisper/Qwen inventerebbero un comando
                            _plog(f"SKIP: solo wake, {spoke_s:.1f}s di voce dopo la wake")
                            ui(lambda: bubble.hide())
                            quiet_until = time.time() + 4.0
                            armed, chunks = False, []
                            rec = KaldiRecognizer(model, SR)
                            if oww_model is not None:
                                oww_model.reset()
                            continue
                        # taglio il silenzio di coda: Whisper non lo serve e la
                        # sua latenza scala con la durata dell'audio
                        cut = 0
                        for i in range(len(chunks) - 1, -1, -1):
                            lv = float(np.sqrt(np.mean(
                                np.frombuffer(chunks[i], "<i2").astype(np.float32) ** 2)))
                            if lv > thr:
                                cut = min(len(chunks), i + 4)  # 0.3 s di coda
                                break
                        if cut == 0:
                            cut = len(chunks)
                        trimmed = b"".join(chunks[:cut])
                        _plog(f"SEND: {dur:.1f}s -> "
                              f"{len(trimmed) / 2 / SR:.1f}s dopo il taglio")
                        ui(lambda: (set_mic_color(ACCENT),
                                    bubble.show(W("understand"), sticky=True)))
                        quiet_until = time.time() + 4.0  # la risposta parlata non deve riarmarmi
                        _passive_send_wav(trimmed)
                        armed, chunks = False, []
                        rec = KaldiRecognizer(model, SR)
                        if oww_model is not None:
                            oww_model.reset()
                    elif dur < 0.6 and since_voice >= 2.0:
                        armed, chunks = False, []  # nessuno ha parlato dopo la wake word
                        rec = KaldiRecognizer(model, SR)
                        if oww_model is not None:
                            oww_model.reset()
                        ui(lambda: bubble.show(W("nothing")))
                    continue
                chunks.append(pcm)
                if len(chunks) > 32:
                    chunks.pop(0)
                txt = rtxt(rec, "FinalResult") if rec.AcceptWaveform(pcm) else rtxt(rec, "PartialResult")
                if txt:
                    _plog(f"visto: {txt!r} (livello {level:.0f})")
                elif time.time() - last_status >= 5.0:
                    last_status = time.time()
                    _plog(f"vivo: livello {level:.0f} soglia {thr:.0f} "
                          f"guadagno {agc_gain:.1f}x fondo {noise:.0f}")
                wake = None
                if time.time() >= quiet_until:
                    if txt and _wake_hit(txt):
                        wake = f"vosk {txt!r}"
                    elif oww_model is not None:
                        # patience=2: due predizioni di fila sopra soglia (~200 ms),
                        # scarta i picchi sporadici di rumore
                        oww_hits = oww_hits + 1 if oww_s >= oww_thr else 0
                        if oww_hits >= 2:
                            wake = f"oww {oww_s:.2f}"
                else:
                    oww_hits = 0
                if wake:
                    _plog(f"WAKE[{wake}]")
                    chunks = chunks[-PRE_WAKE:] + [pcm]  # wake + poco contesto
                    armed, since_voice, spoke_s = True, 0.0, 0.0
                    rec = KaldiRecognizer(model, SR)
                    oww_hits = 0
                    if oww_model is not None:
                        oww_model.reset()
                    ui(lambda: (set_mic_color(RED),
                                bubble.show(W("i_listen"), sticky=True)))
    except Exception as exc:
        _plog(f"ERRORE: {_err_text(exc)}")
        if _passive["on"]:
            ui(lambda: bubble.show(f"Ascolto passivo fermo ({_err_text(exc)})"))
    finally:
        _passive["on"] = False
        ui(lambda: set_mic_color(ACCENT))
        ui(lambda: _animate())  # ritorno dolce al cerchio fermo (anello spento)
        if not listen_disabled["on"]:
            ui(lambda: root.after(2500, _ensure_passive))


def _ensure_passive():
    """Riattiva il ciclo passivo se disattivato, spento e libero il microfono."""
    if not listen_disabled["on"] and not _passive["on"] and not rec_flag.is_set():
        threading.Thread(target=_passive_loop, daemon=True).start()


if not listen_disabled["on"]:
    root.after(1200, lambda: threading.Thread(target=_passive_loop, daemon=True).start())
else:
    _plog("ascolto passivo OFF all'avvio (pulsante microfono barrato): la wake word non ascolta")


def toggle_recording():
    if rec_flag.is_set():
        rec_flag.clear()
        return
    stop_tts()  # stai per parlare: zitta subito la voce
    rec_flag.set()
    set_mic_color(RED)
    bubble.show(W("listen_click"), sticky=True)
    threading.Thread(target=_rec_thread, daemon=True).start()


# --- trascinamento + click sul cerchio ----------------------------------------
# Il microfono si attiva SOLO con un click fermo: trascinare sposta il widget
# e non fa nulla d'altro. Soglia calcolata sulla distanza TOTALE dal punto
# di pressione, cosi' anche i drag lenti vengono riconosciuti come drag.
drag = {"px": 0, "py": 0, "moved": False, "down": False}
DRAG_THRESHOLD = 6


def on_press(e):
    drag.update(px=e.x_root, py=e.y_root, moved=False, down=True)
    _animate(PRESS_SCALE)


def on_motion(e):
    if not drag["down"]:
        return
    if (abs(e.x_root - drag["px"]) + abs(e.y_root - drag["py"])) > DRAG_THRESHOLD:
        if not drag["moved"]:
            _animate(DRAG_SCALE)
        drag["moved"] = True
    if drag["moved"]:
        dx, dy = e.x_root - drag["px"], e.y_root - drag["py"]
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")
        drag["px"], drag["py"] = e.x_root, e.y_root


def on_release(e):
    if not drag["down"]:
        return
    drag["down"] = False
    _animate(HOVER_SCALE if mic_hover["on"] else 1.0)
    if drag["moved"]:
        _save_prefs()  # merge: il mute salvato non si perde piu'
        return
    # click vs doppio click: attendo 260 ms; se arriva il doppio, annullo il toggle
    click = {"seq": getattr(on_release, "_n", 0) + 1}
    on_release._n = click["seq"]
    root.after(260, lambda: toggle_recording() if on_release._n == click["seq"] else None)


# --- log eventi UI (come passive_log: serve a diagnosticare a distanza) -------
_WLOG = BASE / "widget_debug.txt"


def _wlog(msg: str) -> None:
    try:
        if _WLOG.exists() and _WLOG.stat().st_size > 1_000_000:
            _WLOG.unlink()
        with _WLOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


def _quit(_e=None):
    import traceback as _t4
    _wlog("quit da: " + " | ".join(
        l.strip() for l in _t4.format_stack(limit=6) if ".py" in l)[-300:])
    listen_disabled["on"] = True  # ferma il ciclo passivo
    _passive["on"] = False
    rec_flag.clear()
    stop_tts()
    root.destroy()


canvas.bind("<Button-1>", on_press)
canvas.bind("<B1-Motion>", on_motion)
canvas.bind("<ButtonRelease-1>", on_release)
canvas.bind("<Button-3>", _quit)  # click destro = chiudi


# --- avvio server in background: la UI compare subito -------------------------
def _boot():
    if server_up():
        return
    ui(lambda: bubble.show(W("starting"), sticky=True))
    ensure_server()
    ok = server_up()
    ui(lambda: bubble.show(W("ready") if ok else W("srv_down")))


root.after(30, _pump_ui)
root.after(200, lambda: threading.Thread(target=_boot, daemon=True).start())
root.mainloop()
