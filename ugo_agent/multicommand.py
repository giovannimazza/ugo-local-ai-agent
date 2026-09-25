# -*- coding: utf-8 -*-
"""Comandi multipli ("misti"): una frase, piu' azioni.

  "apri youtube e discord"               -> ["apri youtube", "apri discord"]
  "chiudi spotify e poi steam"           -> ["chiudi spotify", "chiudi steam"]
  "apri youtube, discord e steam"        -> ["apri youtube", "apri discord", "apri steam"]
  "metti il volume al 30 e apri spotify" -> ["metti il volume al 30", "apri spotify"]
  "muto e apri spotify"                  -> ["muto", "apri spotify"]

Il principio e' CONSERVATIVO: split_commands() restituisce piu' comandi solo
quando e' sicuro; in qualunque dubbio restituisce la frase intera (comportamento
di oggi). Le guardie:

  - solo comandi "d'azione" si spezzano: apri/chiudi app-siti, volume, esci;
    crea/elimina/aggiungi/leggi/cerca/appunta NON si toccano (la congiunzione
    fa parte del contenuto: "crea un file chiamato spesa e pane", "cerca
    ricette di pasta e pomodori")
  - i frammenti privi di verbo ereditano quello del comando precedente SOLO se
    e' un verbo di lancio/chiusura ("apri youtube e discord" -> "apri discord");
    altrimenti si rinuncia allo split
  - i frammenti privi di lettere o troppo corti invalidano lo split ("volume
    al 30 e 50", "tra le 3 e le 5")
  - frasi gia' separate da ';' (routine), domande, frasi tecniche (Ollama,
    Qwen) e code di cortesia ("per favore") non si spezzano

L'input resta italiano (i comandi in inglese vengono tradotti in italiano da
Qwen prima dell'esecuzione, quindi lo splitter vede sempre l'italiano).

Solo stdlib: importabile senza le dipendenze del server (testabile da solo).
"""
import re

MAX_SUBCOMMANDS = 5

# verbi che APRONO la frase spezzabile (solo azioni "lancio/regolazione")
_CMD_START_RE = re.compile(
    r"^(?:apri|lancia|avvia|chiudi|chiudere|termina|arresta|esci|"
    r"metti|imposta|alza|abbassa|aumenta|diminuisci|riduci|muto|mute|silenzia|"
    r"vai|portami|torna)\b.*", re.IGNORECASE)

# verbi che si possono ripetere sui frammenti senza verbo
_RIPETI_VERBI = ("apri", "lancia", "avvia", "chiudi", "termina",
                 "metti", "alza", "abbassa", "vai")

# frasi il cui primo verbo VIETA lo split: la congiunzione e' contenuto
# (include le domande/chat: 'qual è la capitale della Francia e della Germania'
# non va spezzato in due 'apri/comandi')
_NO_SPLIT_RE = re.compile(
    r"^(?:crea|creami|nuova?|elimina|cancella|rimuovi|aggiungi|scrivi|leggi|"
    r"appunta|annota|segna|prendi\s+nota|elenca|mostra|lista|cerca|ricerca|"
    r"dimmi|che|cosa|quali|quando|quanto|qual|come|perche|chi)\b", re.IGNORECASE)

# code di cortesia / rumore: un frammento composto solo da questi si scarta
_NOISE_RE = re.compile(
    r"^(?:per\s+favore|grazie|dai|su|adesso|ora|subito|un\s+attimo)\W*$",
    re.IGNORECASE)

# frammenti che iniziano con verbi/pronomi: NON ereditano il verbo precedente
# ('apri youtube e leggilo' -> 'apri leggilo' sarebbe una mostruosita')
_VERBISH_RE = re.compile(
    r"^(?:leggi|apri|crea|cerca|scrivi|elimina|chiudi|metti|alza|abbassa|"
    r"dimmi|aggiungi|segna|mostra|elenca|esci|lancia|avvia|termina|prendi|"
    r"controlla|guarda|rimuovi|cancella|appunta|annota|"
    r"mi|ti|ci|vi|si|ne|gli|lo|la|li|l'|il|lo)", re.IGNORECASE)

# congiunzioni e virgole: 'e', 'ed', 'poi', 'e poi', ','
_SPLIT_RE = re.compile(
    r"\s*(?:,\s*)?\b(?:e\s+poi|ed?\s+poi|poi|ed|e)\b\s*|\s*,\s*",
    re.IGNORECASE)


# residui di wake word: 'ehi ugo apri youtube e discord' si pulisce prima
_WAKE_RESIDUE = re.compile(
    r"^\s*(?:(?:ehi|oh|hey|e|a|he)\s+)?"
    r"(?:ugo|hugo|sugo|wugo|yugo|jugo|ugoo|uugo|uhgo)\b[,\s]*",
    re.IGNORECASE)


def _clean(part: str) -> str:
    return part.strip(" \t.,!?;:").strip()


def _plausible(part: str) -> bool:
    """Un frammento vale come comando solo se non e' vuoto/numerico puro."""
    return len(part) >= 4 and bool(re.search(r"[a-zA-Zà-ùÀ-Ù]", part))


def split_commands(text: str) -> list[str]:
    """Spezza una frase in piu' comandi; mai in dubbio: ritorna [text]."""
    t = (text or "").strip().strip(".!?")
    if not t:
        return []
    prev = None
    while prev != (t := _WAKE_RESIDUE.sub("", t, count=1).strip()):
        prev = t
    if not t:
        return [text.strip()]
    low = t.lower()
    # gia' una lista ';' separata (routine), domanda o frase tecnica: no split
    if ";" in t or "?" in t or "ollama" in low or "qwen" in low:
        return [t]
    # domande/chat ("qual è...", "quanto fa...") e verbi di contenuto: la
    # congiunzione e' quasi sempre parte della frase, non un separatore
    if _NO_SPLIT_RE.match(t):
        return [t]
    parts = [p for p in _SPLIT_RE.split(t) if p and p.strip()]
    if len(parts) < 2:
        return [t]

    out, prev = [], ""
    for p in parts:
        p = _clean(p)
        if not p or _NOISE_RE.match(p):
            continue
        if not _plausible(p):
            return [t]                 # 'volume al 30 e 50', 'tra le 3 e le 5'
        if _CMD_START_RE.match(p):
            out.append(p)
            prev = p.lower()
        else:
            if _NO_SPLIT_RE.match(p):
                # il frammento e' un COMANDO DIVERSO col verbo portatore di
                # contenuto ("apri youtube e segna che domani..."): la
                # congiunzione potrebbe essere sua -> niente split
                return [t]
            verb = prev.split(" ", 1)[0] if prev else ""
            if (verb in _RIPETI_VERBI and not _VERBISH_RE.match(p)
                    and len(p.split()) <= 4):
                out.append(f"{verb} {p}")
                prev = f"{verb} {p.lower()}"
            else:
                return [t]             # frammento non interpretabile: niente split
    if len(out) < 2:
        return [t]
    return out[:MAX_SUBCOMMANDS]
