# -*- coding: utf-8 -*-
"""Shim retrocompatibile: il codice vive ora in ugo_agent.tools.ww_train.

`python -m ugo_agent.ww_train` continua a funzionare come documentato.
"""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "tools" / "ww_train.py"),
               run_name="__main__")
