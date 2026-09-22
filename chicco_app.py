# -*- coding: utf-8 -*-
"""
Avvio di Chicco con un solo file: apre server E widget insieme.

  - doppio click su questo file (Windows: lo apre senza finestra)
  - oppure:  python chicco_app.py          (niente console con: pythonw chicco_app.py)
  - se una istanza precedente e' attiva (porta occupata, widget gia' in esecuzione)
    viene terminata in automatico e tutto riparte pulito.

Per avere il comando `chicco` globale:  chicco setup   (o chicco run)
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chicco_agent import launcher  # noqa: E402

if __name__ == "__main__":
    only_server = "--server" in sys.argv
    reuse = "--reuse" in sys.argv  # non riavvia il server se e' gia' attivo e sano
    rc = launcher.start_all(reuse=reuse, skip_widget=only_server)
    if rc != 0 and not (sys.platform == "win32"):
        input("\nPremi INVIO per chiudere...")  # su mac/linux da doppio click
    sys.exit(rc)
