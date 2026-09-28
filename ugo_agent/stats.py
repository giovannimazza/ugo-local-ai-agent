# -*- coding: utf-8 -*-
"""Dashboard statistiche di Ugo: registri di latenze per fase della pipeline,
token/s LLM, log catturati (stdout del server) e composizione del pannello
📊 "usi e consumi" della web UI.

Modulo estratto da server.py (che importa * da qui): le firme pubbliche
restano identiche, quindi nessun cambio di comportamento.
"""
import threading
import time
from collections import deque
from datetime import datetime

# ---------------------------------------------------------------------------
# Registri latenze per fase (vosk, whisper, nemotron, qwen_*, tts, command...)
# ---------------------------------------------------------------------------
_stats_lock = threading.Lock()
_stats: dict[str, list[float]] = {}
_stats_ts: dict[str, list[float]] = {}   # timestamp di cattura, parallelo a _stats
_tps: dict[str, list] = {}        # token/s delle fasi LLM (da eval_count/eval_duration)
_stats_enabled = {"on": True}     # la UI puo' sospendere la raccolta
_boot_ts = time.time()            # per etichettare il cold-start dei motori STT
COLD_WINDOW = 90.0                # secondi: i campioni STT qui dentro sono "cold"
STT_STAGES = ("vosk", "whisper", "nemotron", "quick")

# ---------------------------------------------------------------------------
# Coda circolare del log del server (alimentata dal tee su stdout)
# ---------------------------------------------------------------------------
LOG_KEEP = 400
_log_lock = threading.Lock()
_logq: deque = deque(maxlen=LOG_KEEP)


def boot_age() -> float:
    """Secondi dall'avvio del processo server (per il cold-start)."""
    return time.time() - _boot_ts


def _track(stage: str, dt: float) -> None:
    """Registra la durata (secondi) di una fase; i contatori globali di
    processo (rss, cpu) vengono campionati al momento della richiesta stats."""
    if not _stats_enabled["on"]:
        return
    with _stats_lock:
        buf = _stats.setdefault(stage, [])
        buf.append(dt)
        if len(buf) > 50:
            del buf[:-50]
        tbuf = _stats_ts.setdefault(stage, [])
        tbuf.append(time.time())
        if len(tbuf) > 50:
            del tbuf[:-50]


def _track_tps(stage: str, eval_count: int, eval_duration_ns: int) -> None:
    """Token/s reali riportati da Ollama per la chiamata appena conclusa."""
    if not _stats_enabled["on"] or not eval_count or not eval_duration_ns:
        return
    tps = eval_count / (eval_duration_ns / 1e9)
    with _stats_lock:
        buf = _tps.setdefault(stage, [])
        buf.append(tps)
        if len(buf) > 50:
            del buf[:-50]


def log_line(line: str) -> None:
    """Accoda una riga di log del server (chiamata dal tee su stdout)."""
    line = line.rstrip()
    if not line:
        return
    with _log_lock:
        _logq.append(time.strftime("[%H:%M:%S] ") + line)


def recent_logs(limit: int = 80) -> list[str]:
    """Ultime `limit` righe di log, dalla piu' recente alla piu' vecchia."""
    with _log_lock:
        items = list(_logq)
    return items[-limit:][::-1]


def capture_prints() -> None:
    """Devia stdout del processo verso la coda circolare (e avanti alla
    console reale): cosi' i print di boot/pipeline finiscono in /api/logs.
    Instabilo una sola volta all'avvio del server."""
    import sys
    real = sys.stdout
    buf = {"s": ""}

    class _Tee:
        def write(self, s):
            try:
                buf["s"] += s
                while "\n" in buf["s"]:
                    line, buf["s"] = buf["s"].split("\n", 1)
                    log_line(line)
            except Exception:
                pass
            try:
                return real.write(s)
            except Exception:
                return 0

        def flush(self):
            try:
                real.flush()
            except Exception:
                pass

        def __getattr__(self, name):
            return getattr(real, name)

    sys.stdout = _Tee()


# ---------------------------------------------------------------------------
# API FastAPI (router montato dal server con app.include_router(router))
# ---------------------------------------------------------------------------
try:
    from fastapi import APIRouter
    router = APIRouter()
except Exception:                     # import minimo senza fastapi (test)
    router = None


def _json_ok(payload: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(payload)


def _stage_entry(vals: list[float], tss: list[float], stage: str) -> dict:
    """Statistiche di una fase. Per i motori STT il primo giro dopo l'avvio
    include il caricamento del modello (es. Nemotron ~29 s): quei campioni
    'cold' NON entrano nella media se esistono campioni a regime; restano
    solo contati in cold_n cosi' la dashboard resta onesta."""
    pairs = list(zip(vals, tss))
    if stage in STT_STAGES:
        warm = [d for d, t in pairs if t - _boot_ts >= COLD_WINDOW]
        cold_n = len(pairs) - len(warm)
    else:
        warm = [d for d, _ in pairs]
        cold_n = 0
    use = warm if (warm and cold_n) else [d for d, _ in pairs]
    s = sorted(use)
    p95 = s[min(len(s) - 1, int(round(0.95 * len(s))) - 1)] if s else 0.0
    entry = {"n": len(pairs), "avg": round(sum(use) / len(use), 3) if use else 0.0,
             "p95": round(p95, 3), "max": round(max(use), 3) if use else 0.0,
             "last": round(vals[-1], 3)}
    if cold_n:
        entry["cold_n"] = cold_n
    return entry


if router is not None:
    @router.get("/api/logs")
    def api_logs(limit: int = 80, q: str = ""):
        """Ultime righe di log del server (piu' recenti prima), con filtro
        opzionale per sottostringa (es. /api/logs?q=fastlane)."""
        lines = recent_logs(min(max(limit, 1), 400))
        if q:
            ql = q.lower()
            lines = [ln for ln in lines if ql in ln.lower()]
        return _json_ok({"lines": lines})

    @router.get("/api/stats")
    def api_stats():
        """Dashboard latenze: medie/p95 per fase (ultimi 50 campi ciascuna),
        modelli attivi e stato del processo. Leggero: nessun lavoro pesante."""
        with _stats_lock:
            snap = {k: (list(v), list(_stats_ts.get(k, []))) for k, v in _stats.items()}
        out = {k: _stage_entry(v, t, k) for k, (v, t) in snap.items()}
        # ordinamento di pipeline: prima la voce in ingresso, poi l'interpretazione,
        # poi la voce in uscita e il totale
        order = ["vosk", "fastlane", "whisper", "nemotron", "quick",
                 "qwen_normalize", "qwen_intent", "qwen_chat", "qwen_suggest",
                 "tts_piper", "tts_sapi", "command"]
        stages = [{"stage": k, **out[k]} for k in order if k in out]
        stages += [{"stage": k, **v} for k, v in out.items() if k not in order]
        if not _cb_qwen_on():
            # con la correzione AI della trascrizione disattivata le fasi
            # 'qwen_normalize' non vengono piu' attraversate dalla pipeline:
            # non mostriamo i vecchi valori come se fossero attuali
            stages = [s for s in stages if s["stage"] != "qwen_normalize"]
        try:
            eng = (_cb_effective_stt() or {}).get("engine", "")
        except Exception:
            eng = ""
        if eng in STT_STAGES:
            # onesta' della dashboard: mostra SOLO la fase del motore davvero
            # in uso (es. nemotron); i campioni whisper sono residui storici
            # di quando quel motore era attivo, non dati attuali
            stages = [s for s in stages
                      if s["stage"] not in STT_STAGES or s["stage"] == eng]
        with _stats_lock:
            tps = {k: list(v) for k, v in _tps.items()}
        for st in stages:
            v = tps.get(st["stage"])
            if v:
                st["tps_avg"] = round(sum(v) / len(v), 1)
        proc = {}
        try:
            import psutil
            p = psutil.Process()
            mem = p.memory_info().rss
            cpu = p.cpu_percent(interval=None)   # dall'ultimo campione
            proc = {"rss_mb": round(mem / 1048576, 1), "cpu_pct": round(cpu, 1),
                    "threads": p.num_threads()}
        except Exception:
            proc = {}
        eff_stt = _cb_effective_stt()
        return _json_ok({"stages": stages, "process": proc,
                         "tools": _cb_tool_memory(),
                         "models": {"stt": eff_stt["model"],
                                    "stt_engine": eff_stt["engine"],
                                    "llm": _cb_llm_model(),
                                    "tts": _cb_tts_model()},
                         "enabled": _stats_enabled["on"],
                         "qwen_stt": bool(_cb_qwen_on()),
                         "ts": datetime.now().isoformat(timespec="seconds")})

    @router.post("/api/stats")
    def api_stats_toggle(payload: dict):
        """Attiva/sospende la raccolta (la UI la mette in pausa quando vuole).
        Alla RIASSUNZIONE i registri dei motori STT vengono azzerati e riparte
        il riferimento del cold-start: i campioni pre-pausa appartengono a un
        contesto diverso (magari altro motore attivo) e non vanno mescolati."""
        was = _stats_enabled["on"]
        _stats_enabled["on"] = bool(payload.get("enabled", True))
        if _stats_enabled["on"] and not was:
            with _stats_lock:
                for k in STT_STAGES:
                    _stats.pop(k, None)
                    _stats_ts.pop(k, None)
            global _boot_ts
            _boot_ts = time.time()
        return _json_ok({"ok": True, "enabled": _stats_enabled["on"]})


# ---------------------------------------------------------------------------
# Callback impostate dal server (inversione delle dipendenze: il modulo stats
# non conosce la pipeline, il server fornisce le funzioni con init())
# ---------------------------------------------------------------------------
_cb_qwen_on = lambda: True          # noqa: E731
_cb_effective_stt = lambda: {"engine": "whisper", "model": "-"}   # noqa: E731
_cb_llm_model = lambda: "-"         # noqa: E731
_cb_tts_model = lambda: "-"         # noqa: E731
_cb_tool_memory = lambda: []        # noqa: E731


def init(qwen_on, effective_stt, llm_model, tts_model, tool_memory) -> None:
    """Il server fornisce le funzioni della pipeline al momento dell'import."""
    global _cb_qwen_on, _cb_effective_stt, _cb_llm_model, _cb_tts_model, _cb_tool_memory
    _cb_qwen_on = qwen_on
    _cb_effective_stt = effective_stt
    _cb_llm_model = llm_model
    _cb_tts_model = tts_model
    _cb_tool_memory = tool_memory
