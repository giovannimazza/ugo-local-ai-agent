# -*- coding: utf-8 -*-
"""Test di regressione: conferma vocale per i comandi che toccano il disco
arrivati dall'ASCOLTO PASSIVO (wake=1).

Il microfono passivo raccoglie TV e conversazioni: un falso positivo che crea
o elimina file reali non deve mai eseguirsi (visto dal vivo: 'prova.txt'
creato in Documenti da voce in background). Questa suite verifica, chiamando
process() in-process (niente server ne' HTTP):

  1. la classificazione dei comandi (crea/elimina -> conferma; letture -> no)
  2. il comando passivo pericoloso chiede conferma e NON esegue
  3. 'si' esegue davvero il comando in attesa (con un file INESISTENTE: la
     risposta 'Non trovo il file...' prova il percorso esecutivo completo
     senza scrivere nulla sul disco)
  4. 'no' annulla
  5. un nuovo comando fa decadere la conferma pendente ('si' tardivo rifiutato)
  6. via TESTO e via VOCE ATTIVA lo stesso comando NON chiede conferma

Eseguibile senza dipendenze esterne (il TTS di _emit e' try/except nel server):
    python tests/test_passive_confirm.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ugo_agent import server  # noqa: E402

PHANTOM = "promemoria"   # file realistico che non esiste da nessuna parte
                         # (nomi con underscore/numeri: il 3B li ritaglia
                         # nella spec e il test misurerebbe Ollama, non Ugo)
CONFIRM = ("elimina il file " + PHANTOM, "crea un file di testo chiamato " + PHANTOM)


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL"), name, ("" if cond else "| " + str(extra)[:110]))
    return bool(cond)


results = []

# --- 1) classificazione: verbo+oggetto disco vs letture ed elenchi ---
f = server._passive_needs_confirm
results.append(check("classifica: crea/elimina file e cartelle -> conferma",
                     all(f(x) for x in ("crea un file chiamato spesa con dentro latte",
                                        "elimina la cartella prova",
                                        "cancella il documento vecchio",
                                        "elimina il file " + PHANTOM))))
results.append(check("classifica: letture, elenchi e comandi innocui -> no conferma",
                     not any(f(x) for x in ("che ore sono", "apri spotify",
                                            "metti il volume al 30", "muto",
                                            "elenca i file sul desktop",
                                            "leggi il file nota",
                                            "cosa c'e' scritto nel file spesa",
                                            "quali giochi ho su steam"))))

# --- 2) passivo: chiede conferma, NON esegue ---
r = server.process("Ugo " + CONFIRM[0], "passivo")
results.append(check("passivo: chiede conferma",
                     r.get("intent") == "confirm-passivo"
                     and "davvero" in (r.get("assistant") or "").lower()
                     and bool(server._pending.get("text")), r))
results.append(check("passivo: nessuna esecuzione prima del si'",
                     "non trovo" not in (r.get("assistant") or "").lower(), r))

# --- 3) 'si' esegue il comando in attesa (file inesistente: zero scritture) ---
r = server.process("si", "testo")
results.append(check("'si' esegue il comando confermato",
                     "non trovo il file" in (r.get("assistant") or "").lower()
                     and r.get("intent") != "confirm-passivo", r))

# --- 4) 'no' annulla ---
server.process("Ugo " + CONFIRM[0], "passivo")
r = server.process("no", "testo")
results.append(check("'no' annulla",
                     "annullato" in (r.get("assistant") or "").lower()
                     and not server._pending.get("text"), r))

# --- 5) decadenza: un nuovo comando dismette la conferma pendente ---
server.process("Ugo " + CONFIRM[0], "passivo")
server.process("che ore sono", "testo")
r = server.process("si", "testo")
results.append(check("'si' tardivo rifiutato (conferma decaduta)",
                     "nulla da confermare" in (r.get("assistant") or "").lower(), r))

# --- 6) testo e voce attiva: nessuna conferma, esecuzione diretta ---
r = server.process("elimina il file " + PHANTOM, "testo")
results.append(check("testo: esecuzione diretta senza conferma",
                     "non trovo il file" in (r.get("assistant") or "").lower()
                     and r.get("intent") == "delete_file", r))
r = server.process("Ugo " + CONFIRM[0], "voce")
results.append(check("voce attiva: esecuzione diretta senza conferma",
                     "non trovo il file" in (r.get("assistant") or "").lower()
                     and r.get("intent") == "delete_file", r))

passed = sum(results)
print(f"\nRISULTATO: {passed}/{len(results)} test superati")
sys.exit(0 if passed == len(results) else 1)
