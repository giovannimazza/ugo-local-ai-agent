# -*- coding: utf-8 -*-
"""Shim retrocompatibile: il codice vive ora in ugo_agent.tools.ww_collect.

`python -m ugo_agent.ww_collect` continua a funzionare come documentato.
"""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "tools" / "ww_collect.py"),
               run_name="__main__")
