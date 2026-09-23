# -*- coding: utf-8 -*-
"""Shim retrocompatibile: il codice vive ora in ugo_agent.server."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "ugo_agent" / "server.py"),
               run_name="__main__")
