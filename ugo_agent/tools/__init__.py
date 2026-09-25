# -*- coding: utf-8 -*-
"""Strumenti standalone di ugo_agent (non usati dal server/widget in runtime).

- ww_collect: raccoglie le clip vocali per la wake word personalizzata
- ww_train:   addestra il classificatore openWakeWord su quelle clip

Compatibilità: i comandi documentati `python -m ugo_agent.ww_collect` e
`python -m ugo_agent.ww_train` continuano a funzionare tramite gli shim
nella radice del package.
"""
