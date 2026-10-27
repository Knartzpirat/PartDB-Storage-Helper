"""Routen für den Reiter "Einstellungen" und die Statusanzeige (/api/status).

Einbinden in create_app:  routes_einstellungen.register(app, cfg, hooks, transport=transport)
hooks: Objekt oder Dict mit optionalen Callables
  helper_status() -> {"helfer": {...}, "drucker": {...}}   (Agent HELFER)
  on_helper_token_changed()                                 (bestehende Helfer-Verbindungen trennen)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import einstellungen as es
from .config import Config
from .partdb import check_connection

STATUS_TTL = 15.0   # s – Part-DB höchstens so oft wirklich prüfen


class PartDBIn(BaseModel):
    url: str = ""
    token: str | None = None        # None/leer = gespeicherten Token behalten
    ssl_pruefen: bool = True


class PartDBSave(PartDBIn):
    ohne_test: bool = False


class DruckIn(BaseModel):
    modus: str = "lokal"
    direktdruck: bool = False
    drucker: str = ""
    schnitt: list[str] = []


def _hook(hooks: Any, name: str) -> Callable | None:
    if hooks is None:
        return None
    fn = hooks.get(name) if isinstance(hooks, dict) else getattr(hooks, name, None)
    return fn if callable(fn) else None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def register(app: FastAPI, cfg: Config, hooks: Any = None,
             transport: httpx.BaseTransport | None = None) -> None:
    cache: dict[str, Any] = {"zeit": 0.0, "daten": None}
    cache_lock = threading.Lock()

    def invalidate() -> None:
        cache["zeit"], cache["daten"] = 0.0, None

    def bad(e: Exception):
        raise HTTPException(400, str(e))

    def test(req: PartDBIn) -> tuple[str, str, dict]:
        """(url, token, Testergebnis) – Token None/leer = gespeicherter Token."""
        try:
            url = es.normalize_url(req.url)
        except es.SettingsError as e:
            bad(e)
        token = (req.token or "").strip() or cfg.partdb_token
        return url, token, check_connection(url, token, req.ssl_pruefen, transport=transport)

    def public(result: dict) -> dict:
        return {k: result[k] for k in ("ok", "status", "version", "benutzer", "detail")}

    # ------------------------------------------------------------- Einstellungen
    @app.get("/api/einstellungen")
    def settings_get():
        return es.overview(cfg)

    @app.post("/api/einstellungen/partdb/test")
    def settings_partdb_test(req: PartDBIn):
        return public(test(req)[2])

    @app.put("/api/einstellungen/partdb")
    def settings_partdb_save(req: PartDBSave):
        if req.ohne_test:
            try:
                url = es.normalize_url(req.url)
            except es.SettingsError as e:
                bad(e)
            token = (req.token or "").strip() or cfg.partdb_token
        else:
            url, token, result = test(req)
            if not result["ok"]:
                raise HTTPException(400, f"Nicht gespeichert – {result['detail']}")
        if not url:
            raise HTTPException(400, "Die Part-DB-URL fehlt.")
        values = {"url": url, "ssl_pruefen": req.ssl_pruefen}
        if token:
            values["token"] = token
        es.save_section(cfg, "partdb", values)
        invalidate()
        return es.overview(cfg)

    @app.put("/api/einstellungen/druck")
    def settings_print_save(req: DruckIn):
        try:
            modus, schnitt = es.check_print(req.modus, req.schnitt)
        except es.SettingsError as e:
            bad(e)
        es.save_section(cfg, "druck", {"modus": modus, "direktdruck": req.direktdruck,
                                       "drucker": (req.drucker or "").strip(), "schnitt": schnitt})
        return es.overview(cfg)

    @app.post("/api/einstellungen/helfer/token")
    def settings_helper_token():
        token = es.new_helper_token()
        es.save_section(cfg, "helfer", {"token": token})
        fn = _hook(hooks, "on_helper_token_changed")
        if fn:
            try:
                fn()
            except Exception:   # Token ist gespeichert – Trennen ist nur Kür
                pass
        return dict(es.overview(cfg), token=token)

    # ------------------------------------------------------------- Status
    def partdb_status(force: bool) -> dict:
        with cache_lock:
            if not force and cache["daten"] and time.monotonic() - cache["zeit"] < STATUS_TTL:
                return cache["daten"]
            r = check_connection(cfg.partdb_url, cfg.partdb_token, cfg.verify_ssl, transport=transport)
            data = {"status": r["status"], "url": cfg.partdb_url, "version": r["version"],
                    "benutzer": r["benutzer"], "latenz_ms": r["latenz_ms"], "geprueft": _now(),
                    "detail": r["detail"]}
            cache["zeit"], cache["daten"] = time.monotonic(), data
            return data

    def helper_part() -> dict:
        fallback = {"helfer": {"modus": cfg.print_mode, "verbunden": False},
                    "drucker": {"angeschlossen": None}}
        fn = _hook(hooks, "helper_status")
        if not fn:
            return fallback
        try:
            s = fn() or {}
        except Exception as e:
            fallback["helfer"]["detail"] = f"Helferstatus nicht abrufbar: {e}"
            return fallback
        return {"helfer": s.get("helfer", fallback["helfer"]),
                "drucker": s.get("drucker", fallback["drucker"])}

    @app.get("/api/status")
    def status(neu: bool = False):
        return {"partdb": dict(partdb_status(neu)), **helper_part()}
