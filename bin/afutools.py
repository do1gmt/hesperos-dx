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

Schritt 4 (1.8.69): Contest- und DX-Kalender (/contests?days=30, /dx?days=60) einmal am Tag unter
data/afutools_cal.json; vergangene Contests bleiben 14 Tage stehen (für den Hinweis im Scan-Verlauf).

Schritt 5 (1.8.70): seltene DX-Spots (/dx/spots?minuten=60&stufe=ungewoehnlich,selten,sehr_selten) alle 10 min unter
data/afutools_spots.json, zwei Stunden lang gemerkt. Hört Hesperos ein solches Rufzeichen, wird es hervorgehoben
(Stationen, Treffer) und die Beobachtungsliste kann es melden (Ereignis „Seltene DX-Station gehört“).

Schritt 6 (1.8.71): Sichtlinie zu den Relais in der Nähe (/sichtlinie, Höhenmodell DGM200 des BKG, nur Deutschland).
Je Relaisstandort und Band einmal gefragt (höchstens 15 je Viertelstunde), Ergebnis mit kleinem Geländeschnitt dauerhaft unter
data/afutools_los.json – neu nur bei anderem eigenen Standort, anderer Antennenhöhe oder verschobenem Relais.
Der genaue Standort (8-stelliger Locator, optional) bleibt auf dem Pi: nicht im Dashboard, nicht im Log, nicht im
einfachen Backup – er geht nur an afu.tools, weil die Abfrage ihn braucht.

Der Schlüssel liegt in config/web_settings.json unter afutools.api_key, wird nie ans Dashboard gegeben, nie
protokolliert und nicht ins einfache Backup geschrieben (wie die anderen Zugangsdaten). Jede Antwort bringt unter
`quellenangabe` mit, welche Quellen sichtbar zu nennen sind – das Dashboard zeigt genau diese Nennung.
Regeln der API: nur GET, 120 Abfragen je Minute – Hesperos fragt hier ein Mal am Tag.
"""
import json
import re
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
            "wx": (False, "Funkwetter auf der Scan-Seite (Sonne, Magnetfeld, welche Bänder offen sind)"),
            "cal": (False, "Contest- und DX-Kalender (Karte auf der Scan-Seite, Hinweis im Scan-Verlauf, DX-Abzeichen bei Stationen)"),
            "spots": (False, "Seltene DX-Spots: Hervorhebung, wenn Hesperos eine gerade selten gespottete Station hört (Meldung über die Beobachtungsliste)"),
            "los": (False, "Sichtlinie zu den Relais in der Nähe (Gelände dazwischen, nur Deutschland)")}


def public_config(settings=None):
    """Fürs Dashboard: ohne Schlüssel, nur ob einer gesetzt ist, plus Stand des Zwischenspeichers."""
    c = config(settings)
    d = cached()
    return {"has_key": bool(c["api_key"]), "km": c["km"], "radii": list(RADII), "locator": c["locator"],
            "features": c["features"], "feature_labels": {k: v[1] for k, v in FEATURES.items()},
            "spots_min": spots_min(settings), "spot_levels": list(SPOT_LEVELS),
            "los_height": los_conf(settings)["height"], "los_precise": bool(los_conf(settings)["precise"]),
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
    for f in (CACHE, BASE / "data" / "afutools_wx.json", BASE / "data" / "afutools_cal.json", BASE / "data" / "afutools_spots.json",
              BASE / "data" / "afutools_los.json"):
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


# ---------------------------------------------------------------- Schritt 4: Contest- und DX-Kalender
CAL = BASE / "data" / "afutools_cal.json"
CAL_REFRESH_S = 24 * 3600
CAL_KEEP_PAST_S = 14 * 86400
_CAL = {"mtime": None, "data": None}
STUFEN = ("haeufig", "ungewoehnlich", "selten", "sehr_selten")


def _ts(iso):
    """'2026-11-28T00:00:00Z' oder '2026-11-28' → Sekunden (UTC); None, wenn unlesbar."""
    import calendar as _c
    v = str(iso or "")
    try:
        if "T" in v:
            return _c.timegm(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"))
        return _c.timegm(time.strptime(v[:10], "%Y-%m-%d"))
    except ValueError:
        return None


def cal_refresh(settings=None, log=None, force=False):
    c = config(settings)
    if not c["api_key"] or not c["features"]["cal"]:
        return None
    d = cal_cached()
    if not force and (time.time() - float(d.get("tried_ts") or 0) < (RETRY_S if d.get("error") else CAL_REFRESH_S)):
        return d
    new = dict(d, tried_ts=time.time())
    try:
        rc = _get("/contests", {"days": 30}, c["api_key"])
        rd = _get("/dx", {"days": 60}, c["api_key"])
        s = lambda v, n=80: str(v).strip()[:n] if v not in (None, "") else None
        contests = []
        for x in rc.get("contests") or []:
            t0, t1 = _ts(x.get("start") or x.get("date")), _ts(x.get("end"))
            if not t0:
                continue
            contests.append({"key": s(x.get("key"), 60) or s(x.get("name")), "name": s(x.get("name"), 100) or "?",
                             "mode": s(x.get("mode"), 40) or "", "bands": [str(b)[:6] for b in (x.get("bands") or [])][:16],
                             "start": t0, "end": t1 or t0 + 86400, "allday": bool(x.get("allday")), "url": s(x.get("url"), 200)})
        keep = time.time() - CAL_KEEP_PAST_S                  # schon vorbei: 14 Tage stehen lassen (Scan-Verlauf)
        for old in d.get("contests") or []:
            if old.get("end", 0) >= keep and old["end"] < time.time() and not any(n["key"] == old["key"] and n["start"] == old["start"] for n in contests):
                contests.append(old)
        contests.sort(key=lambda x: x["start"])
        dx = []
        for x in rd.get("dxpeditions") or []:
            t0, t1 = _ts(x.get("start")), _ts(x.get("end"))
            if not x.get("call") or not t0:
                continue
            dx.append({"call": s(x["call"], 20).upper(), "start": t0, "end": (t1 or t0) + 86399, "entity": s(x.get("entity")),
                       "dxcc": s(x.get("dxcc"), 10), "cont": s(x.get("cont"), 2), "iota": s(x.get("iota"), 10),
                       "art": s(x.get("art"), 20), "typ": s(x.get("typ"), 20), "unsicher": bool(x.get("unsicher")),
                       "stufe": x.get("stufe") if x.get("stufe") in STUFEN else None, "stufe_grund": s(x.get("stufe_grund"), 200),
                       "qsl": s(x.get("qsl"), 60), "url": s(((x.get("quellen") or [{}])[0] or {}).get("url"), 200)})
        src = attribution(rc.get("quellenangabe"))
        for y in attribution(rd.get("quellenangabe"))["sources"]:
            if not any(z["name"] == y["name"] for z in src["sources"]):
                src["sources"].append(y)
        new = {"fetched": _iso(), "tried_ts": time.time(), "error": None, "contests": contests, "dx": dx,
               "note_contests": s(rc.get("note"), 600), "note_dx": s(rd.get("note"), 300), "attribution": src}
        if log:
            log("INFO", "afutools", "afutools_cal", contests=len(contests), dx=len(dx))
    except Exception as e:
        new["error"] = str(e)[:200]
        if log:
            log("WARN", "afutools", "afutools_failed", what="Kalender", error=new["error"])
    CAL.parent.mkdir(parents=True, exist_ok=True)
    tmp = CAL.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CAL)
    return new


def cal_cached():
    try:
        m = CAL.stat().st_mtime
    except OSError:
        return {}
    if _CAL["mtime"] != m:
        try:
            _CAL["data"] = json.loads(CAL.read_text(encoding="utf-8"))
        except Exception:
            _CAL["data"] = {}
        _CAL["mtime"] = m
    return _CAL["data"] or {}


def calendar_view(now=None):
    """Fürs Dashboard: Contests, die laufen oder in den nächsten 7 Tagen beginnen; DX-Aktivitäten, die gerade laufen
    oder in 14 Tagen beginnen – ohne Dauer-Sonderrufzeichen, die häufig sind (sonst wären es Hunderte)."""
    if not _on("cal"):
        return {"on": False}
    now = now or time.time()
    d = cal_cached()
    cs = [dict(x, running=x["start"] <= now < x["end"]) for x in d.get("contests") or []
          if x["end"] > now and x["start"] < now + 7 * 86400]
    rank = {k: i for i, k in enumerate(STUFEN)}
    dx = [dict(x, running=x["start"] <= now <= x["end"]) for x in d.get("dx") or []
          if x["end"] >= now and x["start"] < now + 14 * 86400 and not (x.get("art") == "dauer" and x.get("stufe") == "haeufig")]
    dx.sort(key=lambda x: (not x["running"], -rank.get(x.get("stufe"), 0), x["start"]))
    return {"on": True, "fetched": d.get("fetched"), "error": d.get("error"), "contests": cs, "dx": dx,
            "note_contests": d.get("note_contests"), "note_dx": d.get("note_dx"), "attribution": d.get("attribution")}


def contests_during(t0, t1, bands):
    """Contests, die sich mit einem Scanlauf (t0..t1, Sekunden) und seinen Bändern überschneiden – für den Hinweis
    im Scan-Verlauf („Contest lief: …“), sonst []."""
    if not _on("cal") or not t0:
        return []
    bands = set(bands or [])
    return [x["name"] for x in cal_cached().get("contests") or []
            if x["start"] < (t1 or t0) and x["end"] > t0 and (not x.get("bands") or bands & set(x["bands"]))]


def dx_active(calls, now=None):
    """{Rufzeichen: DX-Eintrag} für Rufzeichen, die laut Kalender gerade (±1 Tag) aktiv sind; Dauer-Sonderrufzeichen
    mit Stufe „häufig“ zählen nicht."""
    if not calls or not _on("cal"):
        return {}
    now = now or time.time()
    act = {}
    for x in cal_cached().get("dx") or []:
        if x["start"] - 86400 <= now <= x["end"] + 86400 and not (x.get("art") == "dauer" and x.get("stufe") == "haeufig"):
            act.setdefault(x["call"], x)
    out = {}
    for c in calls:
        b = _base(c)
        if c.upper() in act or b in act:
            out[c] = act.get(c.upper()) or act[b]
    return out


# ---------------------------------------------------------------- Schritt 5: seltene DX-Spots
SPOTS = BASE / "data" / "afutools_spots.json"
SPOTS_REFRESH_S = 10 * 60
SPOTS_KEEP_S = 2 * 3600
_SP = {"mtime": None, "data": None, "idx": None}
SPOT_LEVELS = ("ungewoehnlich", "selten", "sehr_selten")


def spots_min(settings=None):
    if settings is None:
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            settings = {}
    v = (settings.get("afutools") or {}).get("spots_min") if isinstance(settings.get("afutools"), dict) else None
    return v if v in SPOT_LEVELS else "selten"


def spots_refresh(settings=None, log=None, force=False):
    c = config(settings)
    if not c["api_key"] or not c["features"]["spots"]:
        return None
    d = spots_cached()
    if not force and (time.time() - float(d.get("tried_ts") or 0) < (RETRY_S if d.get("error") else SPOTS_REFRESH_S)):
        return d
    new = dict(d, tried_ts=time.time())
    try:
        r = _get("/dx/spots", {"minuten": 60, "stufe": ",".join(SPOT_LEVELS), "kompakt": "false", "limit": 1000}, c["api_key"])
        now = time.time()
        sp = {}
        for x in (d.get("spots") or []):                        # bisherige noch zwei Stunden behalten
            if now - x.get("t", 0) < SPOTS_KEEP_S:
                sp[(x["call"], x.get("band"))] = x
        for x in r.get("spots") or []:
            if x.get("stufe") not in SPOT_LEVELS or not x.get("call"):
                continue
            t = _ts(x.get("zeit")) or now
            k = (str(x["call"]).upper()[:20], str(x.get("band") or "")[:6])
            old = sp.get(k)
            if not old or t >= old.get("t", 0):
                sp[k] = {"call": k[0], "band": k[1], "t": t, "freq_khz": _num(x.get("freq_khz")), "modus": str(x.get("modus") or "")[:12],
                         "stufe": x["stufe"], "stufe_grund": str(x.get("stufe_grund") or "")[:200],
                         "entitaet": str(x.get("entitaet") or "")[:60], "kontinent": str(x.get("kontinent") or "")[:2],
                         "spotter": str(x.get("spotter") or "")[:20], "quelle": str(x.get("quelle") or "")[:12]}
        new = {"fetched": _iso(), "tried_ts": now, "error": None, "spots": sorted(sp.values(), key=lambda x: -x["t"]),
               "stufen": {k: str(v)[:80] for k, v in (r.get("stufen") or {}).items()}, "attribution": attribution(r.get("quellenangabe"))}
        if log:
            log("DEBUG", "afutools", "afutools_spots", n=len(new["spots"]))
    except Exception as e:
        new["error"] = str(e)[:200]
        if log:
            log("WARN", "afutools", "afutools_failed", what="DX-Spots", error=new["error"])
    SPOTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SPOTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SPOTS)
    return new


def spots_cached():
    try:
        m = SPOTS.stat().st_mtime
    except OSError:
        return {}
    if _SP["mtime"] != m:
        try:
            _SP["data"] = json.loads(SPOTS.read_text(encoding="utf-8"))
        except Exception:
            _SP["data"] = {}
        _SP["mtime"] = m
        idx = {}
        for x in _SP["data"].get("spots") or []:
            idx.setdefault(x["call"], []).append(x)
        _SP["idx"] = idx
    return _SP["data"] or {}


def rare_spot(call, band=None, now=None, settings=None):
    """Spot-Eintrag, wenn das Rufzeichen (oder sein Grundrufzeichen) in den letzten zwei Stunden ab der eingestellten
    Stufe gespottet wurde – auf demselben Band bevorzugt; sonst None."""
    if not call or not _on("spots", settings):
        return None
    spots_cached()
    idx = _SP["idx"] or {}
    now = now or time.time()
    lv = SPOT_LEVELS.index(spots_min(settings))
    c = str(call).upper()
    cand = [x for x in (idx.get(c) or idx.get(_base(c)) or []) if now - x["t"] < SPOTS_KEEP_S and SPOT_LEVELS.index(x["stufe"]) >= lv]
    if not cand:
        return None
    same = [x for x in cand if band and x.get("band") == band]
    return max(same or cand, key=lambda x: x["t"])


def spot_text(x):
    lab = {"ungewoehnlich": "ungewöhnlich", "selten": "selten", "sehr_selten": "sehr selten"}.get(x["stufe"], x["stufe"])
    return f"{x['entitaet'] or x['call']} – {lab} gespottet" + (f" ({x['band']} {x['modus']})".rstrip(" ") if x.get("band") else "")


# ---------------------------------------------------------------- Schritt 6: Sichtlinie zu den Relais
LOS = BASE / "data" / "afutools_los.json"
LOS_BATCH = 15                              # Abfragen je Viertelstunde (Housekeeping); Gelände ändert sich nicht
LOS_RETRY_S = 6 * 3600                      # fehlgeschlagene Abfrage frühestens nach 6 h wiederholen
LOS_POINTS = 96                             # Punkte im Geländeschnitt (reicht fürs kleine Profil, hält die Datei klein)
REP_HEIGHT_M = 30.0                         # Antennenhöhe der Relais: unbekannt, afu.tools-Voreinstellung
_LS = {"mtime": None, "data": None}
_LOS_LOCK = threading.Lock()
LOC_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2}([0-9]{2}([A-X]{2})?)?)?$")


def los_conf(settings=None):
    if settings is None:
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            settings = {}
    c = settings.get("afutools") if isinstance(settings.get("afutools"), dict) else {}
    precise = str(c.get("los_locator") or "").upper()
    try:
        h = float(c.get("los_height", 10))
    except (TypeError, ValueError):
        h = 10.0
    own = precise if LOC_RE.match(precise) else str(settings.get("my_locator") or "").upper()
    return {"precise": precise if LOC_RE.match(precise) else "", "from": own if LOC_RE.match(own) else "",
            "height": max(0.0, min(h, 500.0))}


def _loc_fix(v):
    """Locator für die Abfrage in der üblichen Schreibweise (JN59hh), leer wenn ungültig."""
    v = str(v or "").strip().upper()
    if not LOC_RE.match(v):
        return ""
    return v[:4] + v[4:6].lower() + v[6:8] + v[8:10].lower()


def _rep_key(r):
    """Ein Ergebnis je Standort und Band: mehrere Relais am selben Mast (2 m, 70 cm …) teilen sich die Abfrage
    – innerhalb eines Bandes ändert die Frequenz an der Fresnelzone praktisch nichts."""
    return f"{_rep_pos(r)}|{r.get('band') or int(r.get('out_hz', 0) // 1_000_000)}"


def _rep_pos(r):
    if r.get("lat") is not None and r.get("lon") is not None:
        return f"{float(r['lat']):.5f},{float(r['lon']):.5f}"
    return _loc_fix(r.get("locator"))


def _in_de(r):
    """Grob: liegt das Relais im Rechteck um Deutschland? (Feine Grenze prüft afu.tools selbst: moeglich=false)"""
    if r.get("lat") is None or r.get("lon") is None:
        return True
    return 47.2 <= float(r["lat"]) <= 55.1 and 5.8 <= float(r["lon"]) <= 15.1


def los_cached():
    try:
        m = LOS.stat().st_mtime
    except OSError:
        return {}
    if _LS["mtime"] != m:
        try:
            _LS["data"] = json.loads(LOS.read_text(encoding="utf-8"))
        except Exception:
            _LS["data"] = {}
        _LS["mtime"] = m
    return _LS["data"] or {}


def _los_item(r, mhz):
    """Ergebnis einer Abfrage: Urteil, Erklärung, engster Punkt und ein kleiner Geländeschnitt."""
    def n(v, d=1):
        try:
            return round(float(v), d)
        except (TypeError, ValueError):
            return None
    ep = r.get("engster_punkt") if isinstance(r.get("engster_punkt"), dict) else {}
    prof = [[n(p.get("km"), 2), n(p.get("hoehe")), n(p.get("sicht")), n(p.get("fresnel"))]
            for p in (r.get("profil") or []) if isinstance(p, dict)][:1024]
    q = r.get("quelle") if isinstance(r.get("quelle"), dict) else {}
    return {"ok": bool(r.get("moeglich", True)), "urteil": str(r.get("urteil") or "")[:20], "text": str(r.get("text") or "")[:300],
            "km": n(r.get("strecke_km"), 2), "fresnel_anteil": n(r.get("anteil_fresnel"), 2),
            "engster": {"km": n(ep.get("km"), 2), "luft": n(ep.get("luft")), "hoehe": n(ep.get("hoehe"))} if ep else None,
            "h_start": n(r.get("hoehe_start_m")), "h_ziel": n(r.get("hoehe_ziel_m")), "mehr_m": n(r.get("zusaetzliche_hoehe_m")),
            "mhz": mhz, "profil": prof, "vermerk": str(q.get("vermerk") or "")[:80], "stand": str(q.get("stand") or "")[:40],
            "ts": time.time()}


def los_refresh(settings=None, log=None, batch=LOS_BATCH):
    """Nächste Relais ohne Ergebnis abfragen. Rückgabe: Anzahl der Abfragen (None, wenn ausgeschaltet)."""
    c = config(settings)
    lc = los_conf(settings)
    if not c["api_key"] or not c["features"]["los"] or not lc["from"]:
        return None
    with _LOS_LOCK:
        d = dict(los_cached())
        sig = f"{lc['from']}|{lc['height']}|{REP_HEIGHT_M}"
        if d.get("sig") != sig:                          # anderer Standort oder Höhe: alles neu
            d = {"sig": sig, "items": {}}
        items = dict(d.get("items") or {})
        reps = cached().get("repeaters") or []
        now, asked, changed = time.time(), 0, False
        for r in reps:
            if asked >= batch:
                break
            k, pos = _rep_key(r), _rep_pos(r)
            if not pos:
                continue
            old = items.get(k)
            if old and old.get("pos") == pos and (not old.get("error") or now - old.get("ts", 0) < LOS_RETRY_S):
                continue
            mhz = round(r["out_hz"] / 1e6, 4)
            if not _in_de(r):
                items[k] = {"pos": pos, "ok": False, "urteil": "", "text": "", "ts": now}
                changed = True
                continue
            asked += 1
            try:
                res = _get("/sichtlinie", {"von": _loc_fix(lc["from"]), "nach": pos, "hoehe_von": lc["height"],
                                           "hoehe_nach": REP_HEIGHT_M, "mhz": min(max(mhz, 0.2), 100000), "punkte": LOS_POINTS}, c["api_key"])
                it = _los_item(res, mhz)
                if not d.get("attribution"):
                    d["attribution"] = attribution(res.get("quellenangabe"))
            except Exception as e:
                it = {"error": str(e)[:200], "ts": now}
                if log:
                    log("WARN", "afutools", "afutools_failed", what="Sichtlinie", error=it["error"])
                if "401" in it["error"] or "429" in it["error"] or "nicht erreichbar" in it["error"]:
                    it["pos"] = pos
                    items[k] = it
                    changed = True
                    break                                   # nicht weiter anrennen
            it["pos"] = pos
            items[k] = it
            changed = True
        keep = {_rep_key(r) for r in reps}
        if any(k not in keep for k in items):              # Relais aus dem Umkreis gefallen
            items = {k: v for k, v in items.items() if k in keep}
            changed = True
        if changed or d.get("items") is None:
            d["items"] = items
            LOS.parent.mkdir(parents=True, exist_ok=True)
            tmp = LOS.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, LOS)
        if asked and log:
            log("DEBUG", "afutools", "afutools_los", n=asked, done=sum(1 for v in items.values() if v.get("urteil")), total=len(reps))
        return asked


def los_for(rep, settings=None, full=False):
    """Ergebnis für ein Relais (kurz; full=True mit Geländeschnitt) oder None."""
    if not _on("los", settings):
        return None
    d = los_cached()
    if d.get("sig", "").split("|")[0] != los_conf(settings)["from"]:
        return None
    it = (d.get("items") or {}).get(_rep_key(rep))
    if not it or it.get("pos") != _rep_pos(rep):
        return None
    if it.get("error"):
        return {"error": True}
    if not it.get("ok"):
        return {"outside": True}
    out = {k: it.get(k) for k in ("urteil", "text", "km", "engster", "mehr_m", "h_start", "h_ziel")}
    if full:
        out.update(profil=it.get("profil"), mhz=it.get("mhz"), vermerk=it.get("vermerk"), stand=it.get("stand"),
                   h_von=los_conf(settings)["height"], h_nach=REP_HEIGHT_M)
    return out


def los_attribution():
    d = los_cached()
    return d.get("attribution")


def los_progress(settings=None):
    d = los_cached()
    reps = cached().get("repeaters") or []
    it = d.get("items") or {}
    return {"done": sum(1 for r in reps if (it.get(_rep_key(r)) or {}).get("urteil") or (it.get(_rep_key(r)) or {}).get("ok") is False),
            "total": len(reps)}
