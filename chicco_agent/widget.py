# -*- coding: utf-8 -*-
"""
Widget desktop flottante dell'assistente vocale (Chicco).

Finestrella senza barra titolo, sempre in primo piano, TRASCINABILE ovunque:
  - cerchio microfono: 1 click = registra, 2o click = invia
    (in hover si gonfia come una bollicina, alla pressione si schiaccia,
     durante la registrazione pulsa un anello rosso)
  - pillola "Scrivi a Chicco...": appare in hover, invio con Enter o col tasto
  - bolla di risposta arrotondata che compare/svanisce in dissolvenza + voce TTS
  - doppio click sul cerchio = info trascrittore
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
    from chicco_agent import platform_utils as pu

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
PORT = 8123
SR = 16000
POS_FILE = BASE / "widget_pos.json"
ROOT_URL = f"http://127.0.0.1:{PORT}"

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
ACCENT, RED = "#7c6cff", "#e5484d"          # viola normale, rosso registrazione
CARD, TXT, MUT = "#181c2f", "#e8e9f3", "#8b90ad"
PILL_BG = "#1b1f33"      # riempimento pillola = sfondo della Entry (devono coincidere)
PILL_EDGE = "#30365a"    # bordo sottile della pillola e della bolla
SPK_BG = "#2a2f4a"       # pulsante altoparlante (voce attiva)
TRANSPARENT = "#010101"  # colore-chiave: invisibile e click-through su Windows
_KEY_RGB = (1, 1, 1)

# ---------------------------------------------------------------------------
# Layout (tutto in pixel, finestra a dimensione FISSA: le zone vuote sono
# trasparenti e click-through, quindi non danno fastidio)
# ---------------------------------------------------------------------------
C = 100                  # lato del canvas del cerchio
BD = 62                  # diametro del cerchio a riposo
HOVER_SCALE = 1.10       # quanto si gonfia in hover
PRESS_SCALE = 0.92       # quanto si schiaccia alla pressione
DRAG_SCALE = 1.05        # "sollevato" mentre lo trascini
S_MIN, S_MAX = 0.88, 1.18
PULSE_N, PULSE_MS = 14, 1200   # fotogrammi e durata dell'anello di registrazione
HALO_N, HALO_MS = 20, 2000     # anello "respirante" dell'ascolto passivo

ENTRY_W, ENTRY_H = 190, 32     # pillola della textbox
SEND_D, SEND_D_HOVER = 24, 27  # tasto invia a riposo / in hover
SPK_D = 26                     # tasto mute TTS
LISTEN_D = 26                  # tasto on/off ascolto passivo (wake word)
GAP = 6

# il cerchio e' centrato sopra il "tappo" destro della pillola
WIN_W = GAP + SPK_D + GAP + LISTEN_D + GAP + ENTRY_W - ENTRY_H // 2 + C // 2
CIRCLE_CX = WIN_W - C // 2
PILL_X = CIRCLE_CX + ENTRY_H // 2 - ENTRY_W
PILL_Y = C - 4
SPK_X = PILL_X - GAP - SPK_D
LISTEN_X = SPK_X - GAP - LISTEN_D
WIN_H = PILL_Y + ENTRY_H + 4

SS = 4  # supersampling per l'anti-alias


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


def _key(img: Image.Image, thr: int = 110) -> Image.Image:
    """RGBA -> RGB con colore-chiave: niente alpha parziale (limite di Windows)."""
    a = np.asarray(img.convert("RGBA"))
    out = a[..., :3].copy()
    holes = a[..., 3] < thr
    out[holes] = _KEY_RGB
    # un pixel opaco che per caso coincide con la chiave diventerebbe un buco
    clash = (~holes) & np.all(out == _KEY_RGB, axis=-1)
    out[clash] = (2, 2, 2)
    return Image.fromarray(out, "RGB")


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
    return _glossy_ss(d * SS, base, icon).resize((d, d), Image.LANCZOS)


# --- icone (disegnate in bianco su maschera, proporzioni relative al cerchio) ---
def _icon_mic(d, n):
    cx = cy = n / 2
    w, h = n * 0.25, n * 0.28
    top = cy - n * 0.30
    d.rounded_rectangle([cx - w / 2, top, cx + w / 2, top + h], radius=w / 2, fill=255)
    lw = max(2, n * 0.045)
    r = n * 0.20
    acy = cy - n * 0.07
    d.arc([cx - r, acy - r, cx + r, acy + r], start=-35, end=215, fill=255, width=int(lw))
    for ang in (-35, 215):  # estremi dell'archetto arrotondati
        a = np.radians(ang)
        px, py = cx + r * np.cos(a) - lw / 2 * np.cos(a), acy + r * np.sin(a) - lw / 2 * np.sin(a)
        d.ellipse([px - lw / 2, py - lw / 2, px + lw / 2, py + lw / 2], fill=255)
    _rline(d, [(cx, acy + r - lw / 2), (cx, cy + n * 0.185)], lw)
    _rline(d, [(cx - n * 0.105, cy + n * 0.185), (cx + n * 0.105, cy + n * 0.185)], lw)


def _icon_send(d, n):
    cx = cy = n / 2
    lw = n * 0.115
    _rline(d, [(cx, cy + n * 0.21), (cx, cy - n * 0.19)], lw)
    _rline(d, [(cx - n * 0.17, cy - n * 0.02), (cx, cy - n * 0.19), (cx + n * 0.17, cy - n * 0.02)], lw)


def _icon_mic_body(d, n):
    """Corpo del microfono (stesso stile dell'icona mic attiva)."""
    cx = cy = n / 2
    w, h = n * 0.25, n * 0.28
    top = cy - n * 0.30
    d.rounded_rectangle([cx - w / 2, top, cx + w / 2, top + h], radius=w / 2, fill=255)
    lw = max(2, n * 0.045)
    r = n * 0.20
    acy = cy - n * 0.07
    d.arc([cx - r, acy - r, cx + r, acy + r], start=-35, end=215, fill=255, width=int(lw))
    for ang in (-35, 215):
        a = np.radians(ang)
        px, py = cx + r * np.cos(a) - lw / 2 * np.cos(a), acy + r * np.sin(a) - lw / 2 * np.sin(a)
        d.ellipse([px - lw / 2, py - lw / 2, px + lw / 2, py + lw / 2], fill=255)
    _rline(d, [(cx, acy + r - lw / 2), (cx, cy + n * 0.185)], lw)
    _rline(d, [(cx - n * 0.105, cy + n * 0.185), (cx + n * 0.105, cy + n * 0.185)], lw)


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
    return img.resize((w, h), Image.LANCZOS)


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
    # stesso punto di prima: cerchio in basso a destra
    x, y = root.winfo_screenwidth() - 150, root.winfo_screenheight() - 260
    prefs["v"] = 1
if prefs.get("v") == 2:
    # dalla v3 la finestra e' piu' larga (nuovo tasto ascolto passivo a
    # sinistra): sposto l'origine perche' il cerchio resti esattamente dov'era
    x = x - (LISTEN_D + GAP)
    prefs["v"] = 3
elif prefs.get("v") != 3:
    # posizione dalla versione vecchia (finestra 92px, cerchio in 14,10)
    x = x + 14 + 32 - CIRCLE_CX
    y = y + 10 + 32 - C // 2
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

# --- cerchio microfono ---------------------------------------------------------
canvas = tk.Canvas(root, width=C, height=C, bg=TRANSPARENT, highlightthickness=0, bd=0)
canvas.place(x=WIN_W - C, y=0)

# master grandi, poi ridotti a ogni scala: qualita' alta e avvio rapido
_MASTER_D = int(BD * S_MAX) + 1
_mic_master = {
    "idle": _glossy_ss(_MASTER_D * SS, ACCENT, _icon_mic),
    "rec": _glossy_ss(_MASTER_D * SS, RED, _icon_mic),
}
_disc_cache, _ring_cache, _photo_cache = {}, {}, {}


def _disc(state, s100):
    k = (state, s100)
    if k not in _disc_cache:
        d = max(8, int(round(BD * s100 / 100)))
        frame = Image.new("RGBA", (C, C), (0, 0, 0, 0))
        frame.alpha_composite(_mic_master[state].resize((d, d), Image.LANCZOS),
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


def _mic_frame():
    rec = mic_state["color"] == RED
    passive = _passive["on"] and not rec
    s100 = int(round(min(max(anim["s"], S_MIN), S_MAX) * 100))
    k = int(anim["phase"] * PULSE_N) % PULSE_N if rec else -1
    hk = int(anim["pphase"] * HALO_N) % HALO_N if passive else -1
    key = ("rec" if rec else "idle", s100, k if rec else hk)
    ph = _photo_cache.get(key)
    if ph is None:
        if len(_photo_cache) > 400:
            _photo_cache.clear()
        frame = _disc(key[0], s100)
        if rec:
            frame = Image.alpha_composite(_ring(k), frame)
        elif passive:
            frame = Image.alpha_composite(_halo(hk), frame)
        ph = _photo_cache[key] = ImageTk.PhotoImage(_key(frame))
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
    _photo_cache[("idle", _s, -1)] = ImageTk.PhotoImage(_key(_disc("idle", _s)))

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
entry_frame = tk.Canvas(root, width=ENTRY_W, height=ENTRY_H, bg=TRANSPARENT,
                        highlightthickness=0, bd=0, cursor="xterm")
_SEND_SIZES = [SEND_D, SEND_D + 1, SEND_D + 2, SEND_D_HOVER]
_PILLS = [ImageTk.PhotoImage(_key(_rounded_panel(ENTRY_W, ENTRY_H, ENTRY_H // 2, PILL_BG,
                                                 PILL_EDGE, send_d=sd)))
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
PLACEHOLDER = "Scrivi a Chicco…"
ph = {"on": False}
_NAV_KEYS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Left", "Right",
             "Up", "Down", "Home", "End", "Tab", "Escape", "Return", "Caps_Lock", "Win_L", "Win_R"}


def _ph_show():
    if not entry.get():
        ph["on"] = True
        entry.config(fg=MUT)
        entry.insert(0, PLACEHOLDER)
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
    (False, False): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, ACCENT, _icon_mic_on))),
    (False, True): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, _lighter(ACCENT, 0.12), _icon_mic_on))),
    (True, False): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, SPK_BG, _icon_mic_off))),
    (True, True): ImageTk.PhotoImage(_key(_glossy(LISTEN_D, _lighter(SPK_BG, 0.12), _icon_mic_off))),
}
listen_hover = {"on": False}
listen_btn = tk.Label(root, bd=0, bg=TRANSPARENT, cursor="hand2")


def _listen_refresh():
    listen_btn.config(image=_LSN[(listen_disabled["on"], listen_hover["on"])])


_listen_refresh()

def _spk_refresh():
    mute_btn.config(image=_SPK[(tts_muted["on"], spk_hover["on"])])


_spk_refresh()


def open_entry():
    entry_frame.place(x=PILL_X, y=PILL_Y)
    mute_btn.place(x=SPK_X, y=PILL_Y + (ENTRY_H - SPK_D) // 2)
    listen_btn.place(x=LISTEN_X, y=PILL_Y + (ENTRY_H - LISTEN_D) // 2)
    _ph_show()
    entry.focus_set()


def close_entry():
    entry_frame.place_forget()
    mute_btn.place_forget()
    listen_btn.place_forget()
    entry.delete(0, "end")
    ph["on"] = False
    entry.config(fg=TXT)
    _send_hover(False)


def toggle_entry():
    if entry_frame.winfo_ismapped():
        close_entry()
    else:
        open_entry()


# la textbox appare quando il mouse entra nel widget e si chiude quando esce
hover = {"on": False}


def _on_enter(_e=None):
    hover["on"] = True
    if not entry_frame.winfo_ismapped():
        open_entry()


def _on_leave(_e=None):
    hover["on"] = False
    root.after(350, _leave_close)  # piccolo ritardo: evita sfarfallio


def _leave_close():
    if not hover["on"] and entry_frame.winfo_ismapped() and not entry_text():
        close_entry()


for _w in (root, canvas, entry_frame, entry, mute_btn, listen_btn):
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
    bubble.show("Voce disattivata." if tts_muted["on"] else "Voce riattivata.")


mute_btn.bind("<Button-1>", toggle_mute)


def toggle_listen(_e=None):
    listen_disabled["on"] = not listen_disabled["on"]
    _listen_refresh()
    _save_prefs(listen=not listen_disabled["on"])
    if listen_disabled["on"]:
        _passive["on"] = False  # ferma DAVVERO il ciclo di ascolto (bug: prima continuava)
        stop_tts()
        bubble.show("Ascolto passivo disattivato.")
    else:
        bubble.show("Ascolto passivo attivo: dimmi 'Chicco'.")
        threading.Thread(target=_passive_loop, daemon=True).start()


listen_btn.bind("<Button-1>", toggle_listen)


# --- popup: quale trascrittore STT e' attivo (doppio click sul cerchio) -------
def _fetch_json(path):
    with urllib.request.urlopen(ROOT_URL + path, timeout=5) as r:
        return json.loads(r.read().decode())


def _set_model(name):
    """Cambia il modello LLM via server e conferma nella bolla."""
    def run():
        try:
            _post_json("/api/model", {"model": name})
            ui(lambda: bubble.show("Modello AI: " + name.replace("qwen2.5:", "Qwen ")))
        except Exception as exc:
            ui(lambda: bubble.show(f"Errore modello: {exc}"))
    threading.Thread(target=run, daemon=True).start()


def show_stt_popup():
    """Doppio click: menu con info trascrittore + scelta del modello AI."""
    on_release._n = -1  # annulla un eventuale toggle in attesa dal primo click

    def work():
        stt_text = "Trascrittore non disponibile"
        try:
            info = _fetch_json("/api/stt")
            eng = info.get("engine", "?")
            name = {"whisper": "Whisper", "vosk": "Vosk"}.get(eng, eng)
            stt_text = f"{name} · {info.get('model', '?')} · {info.get('device', '?')}"
        except Exception:
            pass
        try:
            mod = _fetch_json("/api/model")
            current, available = mod.get("active", ""), mod.get("available", [])
        except Exception:
            current, available = "", []

        def build():
            m = tk.Menu(root, tearoff=0, font=FONT_UI, bg=CARD, fg=TXT,
                        activebackground=ACCENT, activeforeground="#ffffff")
            m.add_command(label="🎙️ " + stt_text, state="disabled")
            m.add_separator()
            m.add_command(label="Modello AI:", state="disabled")
            for name in available:
                mark = "  ✓ " if name == current else "     "
                m.add_command(label=f"{mark}{name.replace('qwen2.5:', 'Qwen ')}",
                              command=lambda n=name: _set_model(n))
            m.tk_popup(root.winfo_x() + CIRCLE_CX - 60, root.winfo_y() + C // 2)
        ui(build)
    threading.Thread(target=work, daemon=True).start()


canvas.bind("<Double-Button-1>", lambda e: show_stt_popup())


# --- rete ---------------------------------------------------------------------
def _post_json(path, payload):
    req = urllib.request.Request(ROOT_URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def _post_wav(wav: bytes):
    req = urllib.request.Request(ROOT_URL + "/api/listen_wav", data=wav,
                                 headers={"Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def _meta_of(e):
    if e.get("intent") and e["intent"] != "-":
        return f"{e['intent']} · {e.get('detector', '')} · {e.get('ms', 0)} ms"
    return ""


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
            msg = f"Errore: {exc}"  # 'exc' non esiste piu' fuori dall'except
            ui(lambda: bubble.show(msg))

    threading.Thread(target=run, daemon=True).start()


entry.bind("<Return>", lambda e: send_text_cmd())
entry.bind("<Escape>", lambda e: close_entry())


def _on_focus_out(_e=None):
    # la textbox si chiude da sola all'uscita del mouse; il focus-out resta
    # come sicurezza quando si clicca in un'altra app
    if not entry_frame.winfo_ismapped():
        return
    try:
        w = root.focus_get()
        if w is not None and bubble.win is not None and str(w).startswith(str(bubble.win)):
            return
    except Exception:
        pass
    if not entry_text():  # con testo dentro resta aperta
        close_entry()


root.bind("<FocusOut>", _on_focus_out)

# --- registrazione microfono ---------------------------------------------------
rec_flag = threading.Event()


def _pick_mic():
    """Sceglie un microfono che sente davvero.

    `sc.default_microphone()` segue il dispositivo predefinito di Windows, che
    puo' essere un input di linea silenzioso (es. interfaccia audio senza
    nulla collegato). Sonda il rumore di fondo di ogni input e vince il piu'
    vivo; la scelta resta salvata nelle preferenze, cosi' il sondaggio avviene
    una volta sola. Fallback: il microfono predefinito.
    """
    saved = prefs.get("mic")
    if saved:
        for m in sc.all_microphones():
            if m.name == saved:
                return m
    try:
        best, best_rms = None, 0.0
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
        if best is not None and best_rms > 0.002:  # sotto: input morto/silenzioso
            _save_prefs(mic=best.name)
            return best
    except Exception:
        pass
    return sc.default_microphone()


def _rec_thread():
    chunks = []
    try:
        mic = _pick_mic()
        with mic.recorder(samplerate=SR) as rec:
            while rec_flag.is_set():
                chunks.append(rec.record(numframes=SR // 10).copy())
    except Exception as exc:
        rec_flag.clear()
        msg = f"Errore microfono: {exc}"
        ui(lambda: (set_mic_color(ACCENT), bubble.show(msg)))
        return
    audio = np.concatenate(chunks) if chunks else np.zeros((0, 1), np.float32)
    ui(lambda: set_mic_color(ACCENT))
    if audio.size == 0:
        ui(lambda: bubble.show("Non ho registrato nulla."))
        return
    pcm = (np.clip(audio[:, 0], -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    ui(lambda: bubble.show("Trascrivo…", sticky=True))
    try:
        res = _post_wav(buf.getvalue())
        ui(lambda: _show_entry(res))
    except Exception as exc:
        msg = f"Errore server: {exc}"
        ui(lambda: bubble.show(msg))


# --- ascolto passivo (wake word "chicco") con Vosk in streaming ---------------
# Vosk e' leggero (il modello e' gia' in RAM per il fallback STT): ascolta in
# continuo, e quando sente 'chicco' (o varianti) apre la registrazione vera e
# invia l'audio al server. Consuma quasi zero CPU: riconoscitore parziale.
import unicodedata

# grafie tutte normalizzate (minuscolo, senza accenti ne' punteggiatura);
# 'qui quo' e 'kiriko' sono storpiature REALI viste da Vosk
_WAKE_TOK = ("chicco", "chikko", "chiko", "chico", "chiacco", "chichico",
             "cicco", "cikko", "cico", "kicco", "kikko", "kiko",
             "kiriko", "qui quo")
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
        res = _post_wav(buf.getvalue())
        ui(lambda: _show_entry(res))
    except Exception as exc:
        msg = f"Errore server: {exc}"
        ui(lambda: bubble.show(msg))


def _ensure_mic_volume(mic_dev) -> None:
    """Porta il volume di registrazione Windows del microfono USATO a >= 90%:
    matcha il dispositivo per ID endpoint (soundcard e MMDevice condividono
    lo stesso ID) per non regolarmi un input diverso da quello in uso."""
    if not pu.IS_WINDOWS:
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
            ui(lambda: bubble.show("Modello Vosk mancante: ascolto passivo non disponibile."))
            return
        model = VoskModel(str(vosk_dir))
        mic_dev = _pick_mic()
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
        VOICE_LEVEL = 60     # sotto: silenzio (fondo ~2-20); la voce e' oltre ~100
        END_SIL = 1.0        # secondi di silenzio prima di considerare il comando finito
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
                if armed:
                    chunks.append(pcm)
                    since_voice = 0.0 if level > thr else since_voice + 0.1
                    dur = sum(len(c) for c in chunks) / 2 / SR
                    if dur >= 8 or (dur > 0.6 and since_voice > END_SIL):
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
                                    bubble.show("Capisco…", sticky=True)))
                        quiet_until = time.time() + 4.0  # la risposta parlata non deve riarmarmi
                        _passive_send_wav(trimmed)
                        armed, chunks = False, []
                        rec = KaldiRecognizer(model, SR)
                    elif dur < 0.6 and since_voice >= 2.0:
                        armed, chunks = False, []  # nessuno ha parlato dopo la wake word
                        rec = KaldiRecognizer(model, SR)
                        ui(lambda: bubble.show("Non ho sentito niente."))
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
                if txt and time.time() >= quiet_until and _wake_hit(txt):
                    _plog(f"WAKE: {txt!r}")
                    chunks = chunks[-PRE_WAKE:] + [pcm]  # wake + poco contesto
                    armed, since_voice = True, 0.0
                    rec = KaldiRecognizer(model, SR)
                    ui(lambda: (set_mic_color(RED),
                                bubble.show("\U0001F3A4 Ti ascolto…", sticky=True)))
    except Exception as exc:
        _plog(f"ERRORE: {exc}")
        if _passive["on"]:
            ui(lambda: bubble.show(f"Ascolto passivo fermo ({exc})"))
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


def toggle_recording():
    if rec_flag.is_set():
        rec_flag.clear()
        return
    stop_tts()  # stai per parlare: zitta subito la voce
    rec_flag.set()
    set_mic_color(RED)
    bubble.show("\U0001F3A4 Sto ascoltando... clicca di nuovo per inviare", sticky=True)
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


def _quit(_e=None):
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
    ui(lambda: bubble.show("Avvio del server…", sticky=True))
    ensure_server()
    ok = server_up()
    ui(lambda: bubble.show("Pronto." if ok else "Server non raggiungibile."))


root.after(30, _pump_ui)
root.after(200, lambda: threading.Thread(target=_boot, daemon=True).start())
root.mainloop()
