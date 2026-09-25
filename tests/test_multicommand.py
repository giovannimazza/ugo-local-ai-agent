# -*- coding: utf-8 -*-
"""Test dello splitter dei comandi multipli (ugo_agent.multicommand).

Eseguibile senza dipendenze:
    python tests/test_multicommand.py
oppure con pytest dalla root del progetto.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ugo_agent.multicommand import split_commands

CASES = [
    # --- split attesi ---
    ("apri youtube e discord",
     ["apri youtube", "apri discord"]),
    ("chiudi spotify e poi steam",
     ["chiudi spotify", "chiudi steam"]),
    ("apri youtube, discord e steam",
     ["apri youtube", "apri discord", "apri steam"]),
    ("metti il volume al 30 e apri spotify",
     ["metti il volume al 30", "apri spotify"]),
    ("muto e apri spotify",
     ["muto", "apri spotify"]),
    ("alza il volume e poi apri steam",
     ["alza il volume", "apri steam"]),
    ("apri spotify e chiudi discord",
     ["apri spotify", "chiudi discord"]),
    ("vai su gmail e apri github",
     ["vai su gmail", "apri github"]),
    ("ehi ugo apri youtube e discord",          # residuo di wake word
     ["apri youtube", "apri discord"]),
    ("apri steam e poi e poi discord",          # congiunzioni doppie
     ["apri steam", "apri discord"]),
    # --- nessuno split (frase singola) ---
    ("apri youtube", ["apri youtube"]),
    ("volume al 30", ["volume al 30"]),
    ("che ore sono", ["che ore sono"]),
    ("crea una cartella chiamata Prova e pausa sul desktop",
     ["crea una cartella chiamata Prova e pausa sul desktop"]),
    ("cerca gatti e cani buffi su youtube",
     ["cerca gatti e cani buffi su youtube"]),
    ("appunta che domani ho la dentista alle 15 e poi vado dal barbiere",
     ["appunta che domani ho la dentista alle 15 e poi vado dal barbiere"]),
    ("volume tra il 30 e il 50",
     ["volume tra il 30 e il 50"]),
    ("metti il volume al 30 e 50",
     ["metti il volume al 30 e 50"]),
    ("apri youtube e segna che domani ho la dentista",
     ["apri youtube e segna che domani ho la dentista"]),
    ("apri youtube e leggilo",
     ["apri youtube e leggilo"]),
    ("elenca i file sul desktop e poi apri steam",
     ["elenca i file sul desktop e poi apri steam"]),
    ("quando dico modo gaming esegui apri steam; apri discord",
     ["quando dico modo gaming esegui apri steam; apri discord"]),
    ("cosa c'e scritto nel file spesa", ["cosa c'e scritto nel file spesa"]),
    ("", []),
    ("   ", []),
]


def test_split_commands():
    fails = [(t, want, split_commands(t)) for t, want in CASES
             if split_commands(t) != want]
    if fails:
        for t, want, got in fails:
            print(f"FAIL {t!r}\n  atteso:  {want}\n  ottenuto: {got}")
        raise AssertionError(f"{len(fails)}/{len(CASES)} casi falliti")


if __name__ == "__main__":
    test_split_commands()
    print(f"OK: {len(CASES)}/{len(CASES)} casi superati")
