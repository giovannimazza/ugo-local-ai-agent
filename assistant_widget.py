# -*- coding: utf-8 -*-
"""
Widget desktop flottante dell'assistente vocale.

Finestrella senza barra titolo, sempre in primo piano, TRASCINABILE ovunque:
  - cerchio microfono: 1 click = registra, 2o click = invia
  - chip "Scrivi...": si espande in una casella di testo
  - bolla di risposta che svanisce + voce TTS
  - click destro sul cerchio = chiudi il widget

Avvio consigliato (niente console):  pythonw assistant_widget.py
Se il server non e' attivo, lo avvia da solo.
"""
import io
import json
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
import winsound
from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent
PORT = 8123
SR = 16000
POS_FILE = BASE / "widget_pos.json"
ACCENT, RED = "#7c6cff", "#e5484d"
CARD, TXT, MUT = "#181c2f", "#e8e9f3", "#8b90ad"
TRANSPARENT = "#010101"  # colore reso invisibile e click-through su Windows


# ---------------------------------------------------------------------------
# Server: assicura che sia attivo prima di aprire il widget
# ---------------------------------------------------------------------------
def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2):
            return True
    except Exception:
        return False


def ensure_server() -> None:
    if server_up():
        return
    subprocess.Popen(
        [sys.executable, str(BASE / "voice_assistant_server.py")],
        cwd=str(BASE),
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    for _ in range(60):
        time.sleep(1)
        if server_up():
            return


ensure_server()
ROOT_URL = f"http://127.0.0.1:{PORT}"

# ---------------------------------------------------------------------------
# Finestra widget
# ---------------------------------------------------------------------------
W, H_CIRCLE, H_CHIP = 92, 88, 26

root = tk.Tk()
root.overrideredirect(True)          # niente barra titolo
root.attributes("-topmost", True)    # sempre in primo piano
root.attributes("-transparentcolor", TRANSPARENT)  # sfondo invisibile
root.configure(bg=TRANSPARENT)

x, y = None, None
if POS_FILE.exists():
    try:
        pos = json.loads(POS_FILE.read_text())
        x, y = pos.get("x"), pos.get("y")
    except Exception:
        pass
if x is None:
    x = root.winfo_screenwidth() - 150
    y = root.winfo_screenheight() - 260
root.geometry(f"{W}x{H_CIRCLE + H_CHIP}+{x}+{y}")


def ui(fn):
    root.after(0, fn)


# --- bolla di risposta (finestrella sopra il widget, svanisce) ---------------
class Bubble:
    def __init__(self):
        self.win = None
        self.lbl = None
        self.timer = None

    def show(self, text, sticky=False):
        if self.win is None:
            self.win = tk.Toplevel(root)
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.lbl = tk.Label(self.win, text=text, bg=CARD, fg=TXT, justify="left",
                                font=("Segoe UI", 10), wraplength=250, padx=12, pady=9)
            self.lbl.pack()
        else:
            self.lbl.config(text=text)
        self.win.update_idletasks()
        bx = max(0, root.winfo_x() + root.winfo_width() - self.win.winfo_reqwidth())
        by = max(0, root.winfo_y() - self.win.winfo_reqheight() - 10)
        self.win.geometry(f"+{bx}+{by}")
        self.win.deiconify()
        # se la textbox era aperta, riporta il focus all'input (la bolla non deve
        # rubare il focus ne' chiudere la textbox)
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

    def hide(self):
        if self.win:
            self.win.withdraw()


bubble = Bubble()

# --- canvas con cerchio microfono (PNG anti-aliasato, bordi lisci) ----------
canvas = tk.Canvas(root, width=W, height=H_CIRCLE, bg=TRANSPARENT, highlightthickness=0)
canvas.pack(fill="x")


def _make_mic_png(color_hex: str, path: Path) -> None:
    """Cerchio + icona microfono, disegnati 4x e ridotti.

    Il keying di trasparenza di Windows non ha alpha parziale: i pixel di bordo
    semi-trasparenti diventerebbero un contorno nero. Quindi: alpha sotto soglia
    -> colore trasparente esatto; fringe scuro sul bordo -> colore pieno del
    cerchio; icona bianca -> anti-alias normale (interno, opaco).
    """
    D = 64
    S = 4
    img = Image.new("RGBA", (D * S, D * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, D * S - 1, D * S - 1], fill=color_hex)
    cx = cy = D * S / 2
    # microfono bianco, proporzioni ridotte come nell'icona web (24px in un cerchio 64px)
    w = D * S * 0.26
    h = D * S * 0.30
    top = cy - D * S * 0.24
    d.rounded_rectangle([cx - w / 2, top, cx + w / 2, top + h], radius=w / 2, fill="white")
    # archetto inferiore
    r = D * S * 0.21
    arc_cy = cy - D * S * 0.03
    d.arc([cx - r, arc_cy - r, cx + r, arc_cy + r], start=-35, end=215,
          fill="white", width=max(2, int(D * S * 0.045)))
    # stelo e base
    lw = max(2, int(D * S * 0.045))
    d.line([cx, top + h + 1, cx, cy + D * S * 0.21], fill="white", width=lw)
    d.line([cx - D * S * 0.11, cy + D * S * 0.23, cx + D * S * 0.11, cy + D * S * 0.23],
           fill="white", width=lw)
    img = img.resize((D, D), Image.LANCZOS)

    acc = tuple(int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    thr = 120
    out = Image.new("RGB", (D, D), TRANSPARENT)  # niente canale alpha
    op = out.load()
    ip = img.load()
    for yy in range(D):
        for xx in range(D):
            R, G, B, A = ip[xx, yy]
            if A < thr:
                op[xx, yy] = (1, 1, 1)  # esattamente il colore trasparente
                continue
            lum = (R * 0.299 + G * 0.587 + B * 0.114) / 255
            if lum >= 0.55:
                op[xx, yy] = (R, G, B)      # icona bianca: tieni l'anti-alias
            else:
                op[xx, yy] = acc            # bordo/fringe scuro: colore pieno
    out.save(path)


_make_mic_png(ACCENT, BASE / "_mic_on.png")
_make_mic_png(RED, BASE / "_mic_rec.png")
MIC_IMG = tk.PhotoImage(file=str(BASE / "_mic_on.png"))   # noqa: E303
MIC_IMG_RED = tk.PhotoImage(file=str(BASE / "_mic_rec.png"))
img_item = canvas.create_image(14, 10, anchor="nw", image=MIC_IMG)


def set_mic_color(color):
    canvas.itemconfig(img_item, image=(MIC_IMG_RED if color == RED else MIC_IMG))


# --- zona testo: chip compatta che si espande --------------------------------
textbar = tk.Frame(root, bg=TRANSPARENT)
textbar.pack(fill="x")
entry_frame = tk.Frame(textbar, bg=TRANSPARENT)
chip = tk.Label(textbar, text="\U0001F4AC Scrivi...", bg=CARD, fg=MUT,
                font=("Segoe UI", 9), padx=10, pady=4)
chip.pack(anchor="e", padx=6, pady=2)
entry = tk.Entry(entry_frame, bg=CARD, fg=TXT, insertbackground=TXT,
                 relief="flat", font=("Segoe UI", 9))
sendbtn = tk.Label(entry_frame, text="\u27A4", bg=ACCENT, fg="white",
                   font=("Segoe UI", 10, "bold"), padx=7, pady=3)


def open_entry():
    chip.pack_forget()
    entry_frame.pack(fill="x", padx=6, pady=2)
    entry.pack(side="left", fill="x", expand=True, ipady=4)
    sendbtn.pack(side="left", padx=(4, 0), fill="y")
    entry.focus_set()
    root.geometry(f"{W}x{H_CIRCLE + H_CHIP + 30}+{root.winfo_x()}+{root.winfo_y()}")


def close_entry():
    entry_frame.pack_forget()
    chip.pack(anchor="e", padx=6, pady=2)
    entry.delete(0, "end")
    root.geometry(f"{W}x{H_CIRCLE + H_CHIP}+{root.winfo_x()}+{root.winfo_y()}")


def toggle_entry():
    if entry_frame.winfo_ismapped():
        close_entry()
    else:
        open_entry()


chip.bind("<Button-1>", lambda e: toggle_entry())

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
    try:
        winsound.PlaySound(None, 0)
    except Exception:
        pass


def _show_entry(e):
    bubble.show(e.get("assistant") or e.get("error") or "errore")
    winsound.PlaySound(str(BASE / "_tts_reply.wav"),
                       winsound.SND_FILENAME | winsound.SND_ASYNC)


def send_text_cmd():
    text = entry.get().strip()
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
            ui(lambda: bubble.show(f"Errore: {exc}"))

    threading.Thread(target=run, daemon=True).start()


sendbtn.bind("<Button-1>", lambda e: send_text_cmd())
entry.bind("<Return>", lambda e: send_text_cmd())
entry.bind("<Escape>", lambda e: close_entry())

# click fuori dal widget -> il widget perde il focus -> chiudi la textbox.
# Se il focus e' semplicemente passato alla bolla di risposta, non chiudere.
def _on_focus_out(_e=None):
    if not entry_frame.winfo_ismapped():
        return
    try:
        w = root.focus_get()
        if w is not None and bubble.win is not None and str(w).startswith(str(bubble.win)):
            return
    except Exception:
        pass
    close_entry()


root.bind("<FocusOut>", _on_focus_out)

# --- registrazione microfono ---------------------------------------------------
rec_flag = threading.Event()


def _rec_thread():
    chunks = []
    try:
        mic = sc.default_microphone()
        with mic.recorder(samplerate=SR) as rec:
            while rec_flag.is_set():
                chunks.append(rec.record(numframes=SR // 10).copy())
    except Exception as exc:
        rec_flag.clear()
        ui(lambda: (set_mic_color(ACCENT), bubble.show(f"Errore microfono: {exc}")))
        return
    audio = np.concatenate(chunks) if chunks else np.zeros((0, 1), np.float32)
    set_mic_color(ACCENT)
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
        ui(lambda: bubble.show(f"Errore server: {exc}"))


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
DRAG_THRESHOLD = 6  # pixel di distanza totale per considerarlo un drag


def on_press(e):
    drag.update(px=e.x_root, py=e.y_root, moved=False, down=True)


def on_motion(e):
    if not drag["down"]:
        return
    if (abs(e.x_root - drag["px"]) + abs(e.y_root - drag["py"])) > DRAG_THRESHOLD:
        drag["moved"] = True
    if drag["moved"]:
        dx, dy = e.x_root - drag["px"], e.y_root - drag["py"]
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")
        drag["px"], drag["py"] = e.x_root, e.y_root


def on_release(e):
    if not drag["down"]:
        return
    drag["down"] = False
    if drag["moved"]:
        try:
            POS_FILE.write_text(json.dumps({"x": root.winfo_x(), "y": root.winfo_y()}))
        except Exception:
            pass
    else:
        toggle_recording()


canvas.bind("<Button-1>", on_press)
canvas.bind("<B1-Motion>", on_motion)
canvas.bind("<ButtonRelease-1>", on_release)
canvas.bind("<Button-3>", lambda e: root.destroy())  # click destro = chiudi

root.mainloop()
