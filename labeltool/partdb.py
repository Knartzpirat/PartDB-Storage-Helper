"""Schlanker Client für die Part-DB-REST-API (API Platform).

- Auth: Authorization: Bearer <token>
- Accept: application/json  -> Collections kommen als reines JSON-Array
- Pagination: ?page=N&itemsPerPage=M
- Updates: PATCH mit Content-Type application/merge-patch+json
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable

import httpx

PAGE_SIZE = 100
MAX_PAGES = 1000


class PartDBError(RuntimeError):
    pass


def ref_id(ref: Any) -> int | None:
    """ID aus einer Relation holen – egal ob IRI-String, Objekt oder Zahl."""
    if ref is None:
        return None
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref
    if isinstance(ref, str):
        m = re.search(r"/(\d+)$", ref.strip())
        if m:
            return int(m.group(1))
        return int(ref) if ref.isdigit() else None
    if isinstance(ref, dict):
        if ref.get("id") is not None:
            try:
                return int(ref["id"])
            except (TypeError, ValueError):
                pass
        if ref.get("@id"):
            return ref_id(ref["@id"])
    return None


class PartDB:
    def __init__(self, base_url: str, token: str, verify: bool = True,
                 transport: httpx.BaseTransport | None = None):
        if not base_url:
            raise PartDBError("Keine Part-DB-URL in config.yaml (partdb.url) eingetragen.")
        if not token:
            raise PartDBError("Kein API-Token gesetzt (partdb.token oder Umgebungsvariable PARTDB_TOKEN).")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            verify=verify,
            timeout=30.0,
            transport=transport,
        )
        self._lookup_cache: dict[str, dict[int, dict]] = {}

    # ------------------------------------------------------------ low level
    def _check(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            detail = r.text[:500]
            try:
                j = r.json()
                detail = j.get("detail") or j.get("hydra:description") or j.get("description") or detail
            except Exception:
                pass
            raise PartDBError(f"Part-DB {r.request.method} {r.request.url.path} -> {r.status_code}: {detail}")
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def get(self, path: str, **params) -> Any:
        return self._check(self._client.get(path, params=params or None))

    def list(self, path: str, **params) -> list[dict]:
        items: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = self.get(path, page=page, itemsPerPage=PAGE_SIZE, **params)
            if isinstance(data, dict):  # falls doch JSON-LD zurückkommt
                data = data.get("hydra:member") or data.get("member") or []
            if not data:
                break
            items.extend(data)
            if len(data) < PAGE_SIZE:
                break
        return items

    def post(self, path: str, payload: dict) -> Any:
        return self._check(self._client.post(path, json=payload))

    def patch(self, path: str, payload: dict, comment: str | None = None) -> Any:
        params = {"_comment": comment} if comment else None
        return self._check(self._client.patch(
            path, json=payload, params=params,
            headers={"Content-Type": "application/merge-patch+json"},
        ))

    def delete(self, path: str) -> Any:
        return self._check(self._client.delete(path))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PartDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ helpers
    def lookup(self, collection: str) -> dict[int, dict]:
        """Ganze Collection einmal laden und nach ID cachen (Kategorien, Footprints …)."""
        if collection not in self._lookup_cache:
            self._lookup_cache[collection] = {
                i: o for o in self.list(f"/{collection}") if (i := ref_id(o)) is not None
            }
        return self._lookup_cache[collection]

    def name_of(self, ref: Any, collection: str) -> str:
        if isinstance(ref, dict) and ref.get("name"):
            return str(ref["name"])
        rid = ref_id(ref)
        if rid is None:
            return ""
        obj = self.lookup(collection).get(rid)
        return str(obj.get("name", "")) if obj else ""

    def clear_cache(self) -> None:
        self._lookup_cache.clear()

    # ------------------------------------------------------------ domain
    def storage_locations(self) -> list[dict]:
        return self.list("/storage_locations")

    def parts(self) -> list[dict]:
        return self.list("/parts")

    def part_lots(self) -> list[dict]:
        return self.list("/part_lots")

    def create_storage_location(self, name: str, comment: str, parent_id: int | None = None) -> dict:
        payload: dict[str, Any] = {"name": name, "comment": comment}
        if parent_id:
            payload["parent"] = f"/api/storage_locations/{parent_id}"
        return self.post("/storage_locations", payload)

    def update_storage_location(self, location_id: int, payload: dict) -> dict:
        return self.patch(f"/storage_locations/{location_id}", payload, comment="Box geändert (labeltool)")

    def delete_storage_location(self, location_id: int) -> None:
        self.delete(f"/storage_locations/{location_id}")

    def set_part_tags(self, part_id: int, tags: str) -> dict:
        return self.patch(f"/parts/{part_id}", {"tags": tags}, comment="Label gedruckt (labeltool)")

    def lots_by_part(self, parts: Iterable[dict]) -> dict[int, list[dict]]:
        """Part-Lots je Bauteil. Nutzt eingebettete partLots, sonst /part_lots."""
        parts = list(parts)
        result: dict[int, list[dict]] = {}
        missing = False
        for p in parts:
            pid = ref_id(p)
            lots = p.get("partLots")
            if lots is None:
                missing = True
                continue
            resolved = []
            for lot in lots:
                if isinstance(lot, (str, int)):  # nur IRI -> später nachladen
                    missing = True
                    resolved = None
                    break
                resolved.append(lot)
            if resolved is not None and pid is not None:
                result[pid] = resolved
        if missing:
            result = {}
            for lot in self.part_lots():
                pid = ref_id(lot.get("part"))
                if pid is not None:
                    result.setdefault(pid, []).append(lot)
        return result


# ------------------------------------------------------------ Verbindungstest
# Part-DB (API Platform) bietet dafür zwei GET-Endpunkte, beide nur mit gültigem Token:
#   /api/tokens/current -> {"name", "user": {"id", "name"}, "valid_until", "level", ...}
#   /api/info           -> {"version", "title", "default_uri", ...}
# Quelle: src/Entity/UserSystem/ApiToken.php (uriTemplate '/tokens/current.{_format}') und
# src/ApiResource/PartDBInfo.php (uriTemplate '/info.{_format}') im Part-DB-server-Repository.
CHECK_TIMEOUT = 5.0


def _short(r: httpx.Response) -> str:
    try:
        j = r.json()
        if isinstance(j, dict):
            return str(j.get("detail") or j.get("hydra:description") or j.get("title") or "")[:200]
    except Exception:
        pass
    return ""


def _json(r: httpx.Response) -> dict | None:
    try:
        j = r.json()
    except Exception:
        return None
    return j if isinstance(j, dict) else None


def explain_error(exc: Exception, url: str) -> str:
    """Netzwerkfehler als verständlicher deutscher Satz."""
    host = httpx.URL(url).host if url else ""
    text = str(exc)
    low = text.lower()
    causes, c = [], exc
    while c is not None:
        causes.append(c)
        c = c.__cause__ or c.__context__
    if isinstance(exc, httpx.TimeoutException):
        return f"Zeitüberschreitung – {host} antwortet nicht innerhalb von {CHECK_TIMEOUT:.0f} s."
    if "wrong_version_number" in low or "wrong version number" in low:
        return f"{host} spricht an diesem Port kein HTTPS – URL mit http:// statt https:// versuchen."
    if any("ssl" in type(x).__name__.lower() for x in causes) or "certificate" in low or "ssl" in low:
        return (f"SSL-Fehler bei {host}: {text} – bei selbstsigniertem Zertifikat "
                f"\"SSL prüfen\" ausschalten.")
    if ("getaddrinfo" in low or "name or service not known" in low or "nodename nor servname" in low
            or "temporary failure in name resolution" in low or "no address associated" in low):
        return f"Rechnername '{host}' unbekannt (DNS) – URL prüfen."
    if "refused" in low or "10061" in low:
        return f"Verbindung zu {host} abgelehnt – läuft Part-DB, stimmt der Port?"
    if "unreachable" in low or "10051" in low or "10065" in low:
        return f"{host} ist im Netz nicht erreichbar."
    if isinstance(exc, httpx.UnsupportedProtocol) or isinstance(exc, httpx.InvalidURL):
        return f"Ungültige URL: {text}"
    return f"Part-DB nicht erreichbar: {text or type(exc).__name__}"


def check_connection(base_url: str, token: str, verify: bool = True,
                     transport: httpx.BaseTransport | None = None,
                     timeout: float = CHECK_TIMEOUT) -> dict:
    """Nur lesende GET-Anfragen. Ergebnis:
    {"ok", "status": online|offline|token_ungueltig|nicht_konfiguriert,
     "version", "benutzer", "latenz_ms", "detail"}"""
    res = {"ok": False, "status": "nicht_konfiguriert", "version": None, "benutzer": None,
           "latenz_ms": None, "detail": ""}
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return dict(res, detail="Keine Part-DB-URL eingetragen.")
    if not token:
        return dict(res, detail="Kein API-Token eingetragen.")
    try:
        client = httpx.Client(base_url=base_url + "/api", verify=verify, timeout=timeout,
                              transport=transport, follow_redirects=True,
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    except Exception as e:
        return dict(res, status="offline", detail=explain_error(e, base_url))
    with client:
        try:
            t0 = time.perf_counter()
            r = client.get("/tokens/current")
            res["latenz_ms"] = round((time.perf_counter() - t0) * 1000)
            if r.status_code in (401, 403):
                why = _short(r)
                return dict(res, status="token_ungueltig",
                            detail="Part-DB lehnt den API-Token ab (ungültig, abgelaufen oder ohne Rechte)."
                                   + (f" ({why})" if why else ""))
            tok = _json(r) if r.status_code == 200 else None
            if r.status_code == 200 and tok is None:
                return dict(res, status="offline",
                            detail="Unter dieser URL antwortet kein Part-DB (keine JSON-Antwort) – URL prüfen.")
            if r.status_code >= 500:
                return dict(res, status="offline", detail=f"Part-DB meldet einen Serverfehler ({r.status_code}).")

            info_r = client.get("/info")
            info = _json(info_r) if info_r.status_code == 200 else None
            if tok is None:   # ältere Part-DB ohne /tokens/current: /info entscheidet
                if info_r.status_code in (401, 403):
                    return dict(res, status="token_ungueltig",
                                detail="Part-DB lehnt den API-Token ab (ungültig, abgelaufen oder ohne Rechte).")
                if info is None:
                    return dict(res, status="offline",
                                detail=f"Unter dieser URL antwortet kein Part-DB ({r.status_code}) – URL prüfen "
                                       f"(ohne /api).")
        except httpx.HTTPError as e:
            return dict(res, status="offline", detail=explain_error(e, base_url))

        user = (tok or {}).get("user")
        if isinstance(user, dict):
            res["benutzer"] = user.get("name") or user.get("username")
        res["version"] = str(info["version"]) if info and info.get("version") else None
        detail = "Verbunden"
        if res["version"]:
            detail += f" mit Part-DB {res['version']}"
        if res["benutzer"]:
            detail += f" als {res['benutzer']}"
        if tok and tok.get("valid_until"):
            detail += f" (Token gültig bis {str(tok['valid_until'])[:10]})"
        return dict(res, ok=True, status="online", detail=detail + ".")
