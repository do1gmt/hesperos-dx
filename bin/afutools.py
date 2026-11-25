#!/usr/bin/env python3
"""afu.tools – offene Lese-API für Amateurfunkdaten (https://afu.tools/api), 1.8.63.

Schritt 1: Relais im Umkreis des eigenen Locators. Einmal am Tag (oder nach Änderung von Schlüssel, Locator oder
Umkreis) abgeholt und unter data/afutools_repeaters.json zwischengespeichert; daraus
  * Relais-Name an Treffern auf einer Relaisausgabe (nächstes Relais auf der Frequenz),
  * die Liste „Relais in der Nähe“ in den Einstellungen (als eigene Frequenz übernehmen).

Der Schlüssel liegt in config/web_settings.json unter afutools.api_key, wird nie ans Dashboard gegeben, nie
protokolliert und nicht ins einfache Backup geschrieben (wie die anderen Zugangsdaten). Jede Antwort bringt unter
`quellenangabe` mit, welche Quellen sichtbar zu nennen sind – das Dashboard zeigt genau diese Nennung.
Regeln der API: nur GET, 120 Abfragen je Minute – Hesperos fragt hier ein Mal am Tag.
"""
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(os.environ.get("HSP_BASE", Path(__file__).resolve().parent.parent))
SETTINGS = BASE / "config" / "web_settings.json"
CACHE = BASE / "data" / "afutools_repeaters.json"
API = os.environ.get("HSP_AFUTOOLS_URL", "https://afu.tools/api/v1")
UA = "HesperosDX (afu.tools API; https://github.com/do1gmt/hesperos-dx)"
REFRESH_S = 24 * 3600
RETRY_S = 3600                                   # nach einem Fehler frühestens wieder
RADII = (10, 25, 50, 100, 200)
MATCH_HZ = 4000                                  # Treffer gehört zum Relais, wenn so nah an dessen Ausgabe
_LOCK = threading.Lock()
_MEM = {"mtime": None, "data": None}


def config(settings=None):
    if settings is None:
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            settings = {}
    c = settings.get("afutools") if isinstance(settings.get("afutools"), dict) else {}
    km = c.get("km", 50)
    return {"api_key": str(c.get("api_key") or ""), "km": km if km in RADII else 50,
            "locator": str(settings.get("my_locator") or "").upper()}


def public_config(settings=None):
    """Fürs Dashboard: ohne Schlüssel, nur ob einer gesetzt ist, plus Stand des Zwischenspeichers."""
    c = config(settings)
    d = cached()
    return {"has_key": bool(c["api_key"]), "km": c["km"], "radii": list(RADII), "locator": c["locator"],
            "fetched": d.get("fetched"), "count": len(d.get("repeaters") or []), "error": d.get("error"),
            "near": d.get("near"), "km_cached": d.get("km"), "attribution": d.get("attribution")}


def _get(path, params, key, timeout=20):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-API-Key": key, "User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        why = {401: "Schlüssel ungültig oder abgelaufen (afu.tools: 401)", 403: "kein Zugriff (afu.tools: 403)",
               429: "zu viele Abfragen – später wieder (afu.tools: 429)"}.get(e.code)
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            msg = None
        raise RuntimeError(why or f"afu.tools antwortet mit {e.code}" + (f": {msg}" if msg else "")) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"afu.tools nicht erreichbar ({e.reason})") from None


def attribution(q):
    """Pflicht-Nennung aus `quellenangabe`: Text und die Quellen mit Adresse (für Links)."""
    q = q if isinstance(q, dict) else {}
    src = [{"name": "afu.tools", "url": "https://afu.tools"}]
    for ds in q.get("datensaetze") or []:
        for s in ds.get("quellen") or []:
            if s.get("name") and not any(x["name"] == s["name"] for x in src):
                src.append({"name": str(s["name"])[:120], "url": str(s.get("url") or "")[:200]})
    return {"text": str(q.get("nennung") or "Quellen: afu.tools")[:400], "sources": src[:12]}


def _clean(r):
    """Ein Relais aus der Antwort, nur was Hesperos braucht – und plausibel."""
    try:
        out_hz = int(round(float(r["rx_mhz"]) * 1e6))          # rx = was man hört = Ausgabe des Relais
    except (KeyError, TypeError, ValueError):
        return None
    if out_hz <= 0:
        return None
    def f(k):
        try:
            return float(r[k]) if r.get(k) is not None else None
        except (TypeError, ValueError):
            return None
    s = lambda k, n=80: (str(r.get(k)).strip()[:n] if r.get(k) not in (None, "") else None)
    return {"call": s("callsign", 16) or "?", "out_hz": out_hz, "offset_mhz": f("offset_mhz"), "mode": s("mode", 16) or "",
            "band": s("band", 8) or "", "qth": s("qth"), "locator": s("locator", 8), "tone_hz": f("tone_hz"),
            "tone": s("tone_raw", 120), "remarks": s("remarks", 160), "url": s("url", 200),
            "km": f("distance_km"), "bearing": f("bearing_deg"), "lat": f("lat"), "lon": f("lon")}


def refresh(settings=None, log=None, force=False):
    """Relais im Umkreis holen, wenn nötig (älter als ein Tag, anderer Locator/Umkreis, force). Gibt den Stand."""
    c = config(settings)
    with _LOCK:
        d = cached()
        if not c["api_key"] or len(c["locator"]) < 4:
            return d
        same = d.get("near") == c["locator"][:6] and d.get("km") == c["km"]
        age = time.time() - float(d.get("fetched_ts") or 0)
        tried = time.time() - float(d.get("tried_ts") or 0)
        if not force and same and (age < REFRESH_S or (d.get("error") and tried < RETRY_S)):
            return d
        new = dict(d, tried_ts=time.time())
        try:
            r = _get("/repeaters", {"near": c["locator"][:6], "km": c["km"], "limit": 2000}, c["api_key"])
            reps = [x for x in (_clean(e) for e in r.get("repeaters") or []) if x]
            new = {"fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "fetched_ts": time.time(),
                   "tried_ts": time.time(), "near": c["locator"][:6], "km": c["km"], "repeaters": reps,
                   "attribution": attribution(r.get("quellenangabe")), "tone_note": str(r.get("tone_note") or "")[:300],
                   "error": None}
            if log:
                log("INFO", "afutools", "afutools_repeaters", count=len(reps), km=c["km"])
        except Exception as e:
            new["error"] = str(e)[:200]
            if log:
                log("WARN", "afutools", "afutools_failed", what="Relais", error=new["error"])
        _write(new)
        return new


def _write(d):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE)


def cached():
    """Zwischenspeicher, im Speicher gehalten, solange sich die Datei nicht ändert."""
    try:
        m = CACHE.stat().st_mtime
    except OSError:
        return {}
    if _MEM["mtime"] != m:
        try:
            _MEM["data"] = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            _MEM["data"] = {}
        _MEM["mtime"] = m
    return _MEM["data"] or {}


def forget():
    """Schlüssel entfernt: zwischengespeicherte Relais mit löschen (ohne Schlüssel keine Nutzung der Daten)."""
    try:
        CACHE.unlink()
    except OSError:
        pass
    _MEM.update(mtime=None, data=None)


def repeater_at(rf_hz):
    """Nächstes Relais, dessen Ausgabe bei rf_hz liegt (±4 kHz), sonst None."""
    if not rf_hz:
        return None
    reps = [r for r in cached().get("repeaters") or [] if abs(r["out_hz"] - float(rf_hz)) <= MATCH_HZ]
    if not reps:
        return None
    return min(reps, key=lambda r: r["km"] if r["km"] is not None else math.inf)


def repeaters():
    d = cached()
    return {"repeaters": sorted(d.get("repeaters") or [], key=lambda r: (r["km"] if r["km"] is not None else 1e9, r["out_hz"])),
            "fetched": d.get("fetched"), "error": d.get("error"), "attribution": d.get("attribution"),
            "tone_note": d.get("tone_note"), "km": d.get("km"), "near": d.get("near")}
