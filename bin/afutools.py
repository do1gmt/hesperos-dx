#!/usr/bin/env python3
"""afu.tools – offene Lese-API für Amateurfunkdaten (https://afu.tools/api), 1.8.63.

Schritt 1 (1.8.63): Relais im Umkreis des eigenen Locators. Einmal am Tag (oder nach Änderung von Schlüssel, Locator oder
Umkreis) abgeholt und unter data/afutools_repeaters.json zwischengespeichert; daraus
  * Relais-Name an Treffern auf einer Relaisausgabe (nächstes Relais auf der Frequenz),
  * die Liste „Relais in der Nähe“ in den Einstellungen (als eigene Frequenz übernehmen).

Schritt 2 (1.8.64): Locator zu gehörten Rufzeichen, die selbst keinen senden (/qth – aus PSK Reporter und RBN,
nie aus der Rufzeichenliste). Höchstens 100 Rufzeichen je Abfrage, alle 15 min eine; gefundene werden nach 7 Tagen
neu gefragt, nicht gefundene nach 2 Tagen. Ergebnis in der Tabelle afu_qth der Datenbank.

Schritt 3 (1.8.68): Funkwetter (/funkwetter?kurz=true und /funkwetter/offen?von=dl) alle 30 min, zwischengespeichert
unter data/afutools_wx.json – Kopf-Werte, Lage, Bandbewertung und die gemessenen Öffnungen von DL aus.

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
QTH_SRC = BASE / "data" / "afutools_qth_src.json"       # Quellenangabe der letzten /qth-Antwort
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
    f = c.get("features") if isinstance(c.get("features"), dict) else {}
    return {"api_key": str(c.get("api_key") or ""), "km": km if km in RADII else 50,
            "locator": str(settings.get("my_locator") or "").upper(),
            "features": {k: bool(f.get(k, d)) for k, (d, _) in FEATURES.items()}}


# Was Hesperos von afu.tools holt – einzeln abschaltbar, damit das Dashboard nicht überladen wird (1.8.67).
# Neue Teile kommen ausgeschaltet dazu; Relais und Standorte waren schon da und bleiben an.
FEATURES = {"repeaters": (True, "Relais in der Nähe und an Treffern"),
            "qth": (True, "Standort gehörter Stationen ohne eigenen Locator"),
            "wx": (False, "Funkwetter auf der Scan-Seite (Sonne, Magnetfeld, welche Bänder offen sind)")}


def public_config(settings=None):
    """Fürs Dashboard: ohne Schlüssel, nur ob einer gesetzt ist, plus Stand des Zwischenspeichers."""
    c = config(settings)
    d = cached()
    return {"has_key": bool(c["api_key"]), "km": c["km"], "radii": list(RADII), "locator": c["locator"],
            "features": c["features"], "feature_labels": {k: v[1] for k, v in FEATURES.items()},
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
        if not c["api_key"] or len(c["locator"]) < 4 or not c["features"]["repeaters"]:
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
    """Schlüssel entfernt: zwischengespeicherte Daten mit löschen (ohne Schlüssel keine Nutzung der Daten)."""
    for f in (CACHE, BASE / "data" / "afutools_wx.json"):
        try:
            f.unlink()
        except OSError:
            pass
    _MEM.update(mtime=None, data=None)


def repeater_at(rf_hz, settings=None):
    """Nächstes Relais, dessen Ausgabe bei rf_hz liegt (±4 kHz), sonst None (auch wenn „Relais“ abgeschaltet ist)."""
    if not rf_hz or not _on("repeaters", settings):
        return None
    reps = [r for r in cached().get("repeaters") or [] if abs(r["out_hz"] - float(rf_hz)) <= MATCH_HZ]
    if not reps:
        return None
    return min(reps, key=lambda r: r["km"] if r["km"] is not None else math.inf)


def _on(feature, settings=None):
    """Teil eingeschaltet und Schlüssel da? Die Einstellungsdatei wird nur bei Änderung neu gelesen."""
    if settings is None:
        try:
            m = SETTINGS.stat().st_mtime
        except OSError:
            return False
        if _MEM.get("st_mtime") != m:
            _MEM["st"] = config()
            _MEM["st_mtime"] = m
        c = _MEM["st"]
    else:
        c = config(settings)
    return bool(c["api_key"]) and c["features"].get(feature, False)


def repeaters():
    d = cached()
    return {"repeaters": sorted(d.get("repeaters") or [], key=lambda r: (r["km"] if r["km"] is not None else 1e9, r["out_hz"])),
            "fetched": d.get("fetched"), "error": d.get("error"), "attribution": d.get("attribution"),
            "tone_note": d.get("tone_note"), "km": d.get("km"), "near": d.get("near")}


# ---------------------------------------------------------------- Schritt 2: Locator zu Rufzeichen (/qth)
QTH_BATCH, QTH_FOUND_DAYS, QTH_MISS_DAYS, QTH_HEARD_DAYS = 100, 7, 2, 30
_QTH = {"fail_ts": 0.0, "src": None, "src_mtime": None}


def ensure_qth(db):
    db.execute("""CREATE TABLE IF NOT EXISTS afu_qth (call TEXT PRIMARY KEY, loc TEXT, quelle TEXT, alter_tage INTEGER,
                  fetched TEXT)""")


def _iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def _base(call):
    """Rufzeichen für die Abfrage: ohne APRS-SSID (-9) und ohne /P, /M, /QRP."""
    c = call.upper().split("-")[0]
    parts = [p for p in c.split("/") if p]
    return max(parts, key=lambda p: (any(ch.isdigit() for ch in p) and any(ch.isalpha() for ch in p), len(p))) if parts else c


def qth_candidates(db, now=None):
    """Bis zu 100 Stationen der letzten 30 Tage ohne eigenen Locator, die noch nicht (oder zu lange nicht) gefragt wurden."""
    now = now or time.time()
    ensure_qth(db)
    return [r[0] for r in db.execute(
        """SELECT s.call FROM station s LEFT JOIN afu_qth a ON a.call = s.call
           WHERE (s.grid IS NULL OR s.grid = '') AND s.last >= ?
             AND (a.call IS NULL OR (a.loc IS NOT NULL AND a.fetched < ?) OR (a.loc IS NULL AND a.fetched < ?))
           ORDER BY s.last DESC LIMIT ?""",
        (_iso(now - QTH_HEARD_DAYS * 86400)[:19], _iso(now - QTH_FOUND_DAYS * 86400), _iso(now - QTH_MISS_DAYS * 86400),
         QTH_BATCH)).fetchall()]


def qth_refresh(db_connect, settings=None, log=None):
    """Eine Abfrage für die nächsten bis zu 100 Rufzeichen. Gibt (gefragt, gefunden) oder None (nichts zu tun)."""
    c = config(settings)
    if not c["api_key"] or not c["features"]["qth"] or time.time() - _QTH["fail_ts"] < RETRY_S:
        return None
    db = db_connect()
    try:
        calls = qth_candidates(db)
        if not calls:
            return None
        ask = {}
        for call in calls:
            ask.setdefault(_base(call), []).append(call)
        try:
            r = _get("/qth", {"rufe": ",".join(ask)}, c["api_key"])
        except Exception as e:
            _QTH["fail_ts"] = time.time()
            if log:
                log("WARN", "afutools", "afutools_failed", what="Locator", error=str(e)[:200])
            return None
        got = {str(k).upper(): v for k, v in (r.get("qth") or {}).items() if isinstance(v, dict)}
        now, found = _iso(), 0
        for b, orig in ask.items():
            v = got.get(b) or {}
            loc = str(v.get("loc") or "").upper()
            ok = 4 <= len(loc) <= 8 and loc[:2].isalpha() and loc[2:4].isdigit() and "A" <= loc[0] <= "R" and "A" <= loc[1] <= "R"
            found += bool(ok)
            for call in orig:
                db.execute("INSERT OR REPLACE INTO afu_qth (call, loc, quelle, alter_tage, fetched) VALUES (?,?,?,?,?)",
                           (call, loc if ok else None, str(v.get("quelle") or "")[:20] if ok else None,
                            int(v.get("alter_tage") or 0) if ok else None, now))
        db.commit()
        if r.get("quellenangabe"):
            QTH_SRC.parent.mkdir(parents=True, exist_ok=True)
            tmp = QTH_SRC.with_suffix(".tmp")
            tmp.write_text(json.dumps(attribution(r["quellenangabe"]), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, QTH_SRC)
        if log:
            log("INFO", "afutools", "afutools_qth", asked=len(ask), found=found)
        return len(ask), found
    finally:
        db.close()


SRC_NAMES = {"psk": "PSK Reporter", "rbn": "RBN", "log": "Log"}


def qth_for(db, calls):
    """{Rufzeichen: {loc, src, age_days}} für die gefragten Rufzeichen, soweit afu.tools einen Locator kannte
    (leer, wenn „Standorte“ abgeschaltet oder kein Schlüssel da ist)."""
    calls = [c for c in calls if c]
    if not calls or not _on("qth"):
        return {}
    ensure_qth(db)
    out = {}
    for i in range(0, len(calls), 400):
        part = calls[i:i + 400]
        for call, loc, q, age, fetched in db.execute(
                f"SELECT call, loc, quelle, alter_tage, fetched FROM afu_qth WHERE loc IS NOT NULL AND call IN ({','.join('?' * len(part))})", part):
            try:
                since = (time.time() - __import__("calendar").timegm(time.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ"))) / 86400
            except (TypeError, ValueError):
                since = 0
            out[call] = {"loc": loc, "src": SRC_NAMES.get(q, q or "?"), "age_days": int((age or 0) + since)}
    return out


def qth_attribution():
    try:
        m = QTH_SRC.stat().st_mtime
    except OSError:
        return None
    if _QTH["src_mtime"] != m:
        try:
            _QTH["src"] = json.loads(QTH_SRC.read_text(encoding="utf-8"))
        except Exception:
            _QTH["src"] = None
        _QTH["src_mtime"] = m
    return _QTH["src"]


def qth_forget(db):
    ensure_qth(db)
    db.execute("DELETE FROM afu_qth")
    db.commit()
    try:
        QTH_SRC.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- Schritt 3: Funkwetter
WX = BASE / "data" / "afutools_wx.json"
WX_REFRESH_S = 30 * 60
_WX = {"mtime": None, "data": None}


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def wx_refresh(settings=None, log=None, force=False):
    """Funkwetter holen, wenn eingeschaltet und älter als 30 min. Nur was angezeigt wird, kommt in den Speicher."""
    c = config(settings)
    if not c["api_key"] or not c["features"]["wx"]:
        return None
    d = wx_cached()
    if not force and (time.time() - float(d.get("tried_ts") or 0) < (RETRY_S if d.get("error") else WX_REFRESH_S)):
        return d
    new = dict(d, tried_ts=time.time())
    try:
        w = _get("/funkwetter", {"kurz": "true"}, c["api_key"])
        o = _get("/funkwetter/offen", {"von": "dl"}, c["api_key"])
        k = w.get("kopf") or {}
        head = {x: _num(k.get(x)) for x in ("sfi", "ssn", "a", "kp", "muf_dl", "fof2_dl", "wind", "bz", "aurora_eu")}
        head.update({"xray": str(k.get("xray") or "")[:8], "magnetfeld": str(k.get("magnetfeld") or "")[:30],
                     "magnetfeld_en": str(k.get("magnetfeld_en") or "")[:30], "rauschen": str(k.get("rauschen") or "")[:12],
                     "muf_gemessen": bool(k.get("muf_dl_gemessen")), "tag_dl": bool(k.get("tag_dl"))})
        ok = {"gut", "mittel", "schlecht", "zu"}
        bands = [{"band": str(b.get("band"))[:6], "tag": b.get("tag") if b.get("tag") in ok else None,
                  "nacht": b.get("nacht") if b.get("nacht") in ok else None, "jetzt": b.get("jetzt") if b.get("jetzt") in ok else None}
                 for b in w.get("baender") or [] if b.get("band")]
        es = ((w.get("vhf") or {}).get("es") or {})
        matrix = {}
        for band, conts in (o.get("matrix") or {}).items():
            if isinstance(conts, dict):
                matrix[str(band)[:6]] = {str(ct)[:2]: {"empf": int(v.get("empf") or 0), "snr": _num(v.get("snr"))}
                                         for ct, v in conts.items() if isinstance(v, dict)}
        src = attribution(w.get("quellenangabe"))
        for x in attribution(o.get("quellenangabe"))["sources"]:
            if not any(y["name"] == x["name"] for y in src["sources"]):
                src["sources"].append(x)
        new = {"fetched": _iso(), "tried_ts": time.time(), "error": None, "head": head, "lage": str(w.get("lage") or "")[:300],
               "bands": bands, "es": {b: v for b, v in es.items() if v in ok}, "aurora": (w.get("vhf") or {}).get("aurora"),
               "open": {"matrix": matrix, "minuten": int(o.get("minuten") or 30), "sender": int(o.get("sender") or 0),
                        "stand": str(o.get("stand") or "")[:20], "baender": list(o.get("baender") or [])[:20],
                        "kontinente": list(o.get("kontinente") or [])[:8]},
               "generated": str(w.get("generated") or "")[:20], "attribution": src}
        if log:
            log("INFO", "afutools", "afutools_wx", sfi=head["sfi"], kp=head["kp"])
    except Exception as e:
        new["error"] = str(e)[:200]
        if log:
            log("WARN", "afutools", "afutools_failed", what="Funkwetter", error=new["error"])
    WX.parent.mkdir(parents=True, exist_ok=True)
    tmp = WX.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, WX)
    return new


def wx_cached():
    try:
        m = WX.stat().st_mtime
    except OSError:
        return {}
    if _WX["mtime"] != m:
        try:
            _WX["data"] = json.loads(WX.read_text(encoding="utf-8"))
        except Exception:
            _WX["data"] = {}
        _WX["mtime"] = m
    return _WX["data"] or {}


def wx():
    """Fürs Dashboard: Funkwetter aus dem Zwischenspeicher, nur wenn eingeschaltet."""
    if not _on("wx"):
        return {"on": False}
    d = wx_cached()
    return dict({k: v for k, v in d.items() if k != "tried_ts"}, on=True)
