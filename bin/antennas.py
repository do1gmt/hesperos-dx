#!/usr/bin/env python3
"""Antennen: jede Antenne einmal mit Name, Frequenzbereich und (Richtantenne) Richtung, Öffnungswinkel, Erhebung
anlegen (System → Geräte → Antennen). Gespeichert in data/antennas.json.

Ab 1.8.33 gehört die Antenne nicht mehr zum Stick, sondern zum Vorgang: „use“ sagt, welche Antenne der Scan
(mit Überflügen) und welche Live benutzt – ein festes Profil oder „auto“ (nach Band: das Profil, dessen
Frequenzbereich passt, das engste zuerst). Der Frequenzbereich ist nur ein Hinweis, keine Sperre: auch außerhalb
wird gescannt, die Antenne steht dann mit „außerhalb“ dabei. resolve(use, freq) liefert, welche Antenne für eine
Frequenz gilt; Scanner, Überflüge, Live, PSK Reporter und die Richtantennen-Planung lesen nur noch hier.
Später kann ein Profil einen Ausgang eines Antennenumschalters bekommen („switch_port“, noch ohne Wirkung).

Beim ersten Aufruf werden die Profile aus den bisherigen Einträgen angelegt (Antennen der Sticks, PMSDR), die
Wahl für Scan und Live aus der bisherigen Wahl am Haupt-Stick bzw. an der PMSDR.
"""
import json
import re
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FILE = BASE / "data" / "antennas.json"
FIELDS = ("az", "bw", "el", "fmin", "fmax")


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or f"ant-{int(time.time())}"


USES = ("scan", "live")


def load():
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(d.get("profiles"), list):
            if not isinstance(d.get("use"), dict):            # bis 1.8.32: Wahl hing am Stick / an der PMSDR
                d["use"] = _legacy_use(d["profiles"])
                save(d)
            return d
    except Exception:
        pass
    return _initial()


def _legacy_use(profiles):
    """Bisherige Wahl übernehmen: Profil des Haupt-Sticks, sonst der PMSDR, sonst das einzige Profil, sonst auto."""
    ids = [p["id"] for p in profiles]
    cur = _legacy_current()
    pid = next((cur[k] for k in ("rtlsdr", "pmsdr") if cur.get(k) in ids), None)
    if pid is None:
        names = _legacy_names()
        pid = next((p["id"] for p in profiles if p["name"].strip().lower() in names), None)
    if pid is None:
        pid = ids[0] if len(ids) == 1 else "auto"
    return {u: pid for u in USES}


def save(d):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def _initial():
    """Profile aus den bisherigen Angaben (Sticks, PMSDR) – einmalig."""
    profs, seen = [], set()
    try:
        import sticks as STK
        for r in STK.load()["sticks"].values():
            name = (r.get("antenna") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            profs.append({"id": _slug(name), "name": name, "az": r.get("ant_az"), "bw": r.get("ant_bw"),
                          "el": r.get("ant_el"), "fmin": r.get("ant_fmin"), "fmax": r.get("ant_fmax")})
    except Exception:
        pass
    try:
        cfg = json.loads((BASE / "config" / "scanner.json").read_text(encoding="utf-8"))
        name = ((cfg.get("antennas") or {}).get("pmsdr") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            profs.append({"id": _slug(name), "name": name, "az": None, "bw": None, "el": None, "fmin": 0.1, "fmax": 30.0})
    except Exception:
        pass
    d = {"profiles": profs}
    d["use"] = _legacy_use(profs)
    save(d)
    return d


def _num(v, lo, hi, what):
    if v in (None, ""):
        return None
    v = float(str(v).replace(",", "."))
    if not lo <= v <= hi:
        raise ValueError(f"{what} zwischen {lo:g} und {hi:g}")
    return v


def set_profiles(items):
    out, ids = [], set()
    for it in items if isinstance(items, list) else []:
        name = str(it.get("name") or "").strip()[:60]
        if not name:
            continue
        pid = str(it.get("id") or _slug(name))[:40]
        while pid in ids:
            pid += "-2"
        ids.add(pid)
        port = str(it.get("switch_port") or "").strip()[:20] or None
        p = {"id": pid, "name": name, "switch_port": port, "az": _num(it.get("az"), 0, 360, "Richtung (°)"),
             "bw": _num(it.get("bw"), 10, 360, "Öffnungswinkel (°)"), "el": _num(it.get("el"), 0, 90, "Erhebung (°)"),
             "fmin": _num(it.get("fmin"), 0.01, 6000, "Frequenz von (MHz)"), "fmax": _num(it.get("fmax"), 0.01, 6000, "Frequenz bis (MHz)")}
        if p["fmin"] is not None and p["fmax"] is not None and p["fmin"] >= p["fmax"]:
            raise ValueError(f"{name}: Frequenzbereich „von“ muss unter „bis“ liegen")
        out.append(p)
    old = load()
    use = {u: (v if v == "auto" or v in ids else ("auto" if not out else
               (out[0]["id"] if len(out) == 1 else "auto"))) for u, v in (old.get("use") or {}).items()}
    d = {"profiles": out, "use": {u: use.get(u, "auto") for u in USES}}
    save(d)
    return d


def _legacy_current():
    """Bis 1.8.32: gewähltes Profil am Haupt-Stick und an der PMSDR (nur noch zum Übernehmen)."""
    cur = {"rtlsdr": None, "pmsdr": None}
    try:
        import sticks as STK
        main = STK.effective_main()
        if main:
            cur["rtlsdr"] = main.get("ant_profile") or None
    except Exception:
        pass
    try:
        cfg = json.loads((BASE / "config" / "scanner.json").read_text(encoding="utf-8"))
        cur["pmsdr"] = (cfg.get("antenna_profiles") or {}).get("pmsdr")
    except Exception:
        pass
    return cur


def _legacy_names():
    names = set()
    try:
        import sticks as STK
        main = STK.effective_main()
        if main and (main.get("antenna") or "").strip():
            names.add(main["antenna"].strip().lower())
    except Exception:
        pass
    try:
        cfg = json.loads((BASE / "config" / "scanner.json").read_text(encoding="utf-8"))
        n = ((cfg.get("antennas") or {}).get("pmsdr") or "").strip()
        if n:
            names.add(n.lower())
    except Exception:
        pass
    return names


def current():
    """Gewählte Antenne je Vorgang: {"scan": id|"auto"|None, "live": …}."""
    return dict(load().get("use") or {})


def covers(p, freq_hz):
    """Liegt freq_hz im Frequenzbereich des Profils? None = Profil ohne Bereich (gilt überall)."""
    if p.get("fmin") is None or p.get("fmax") is None or freq_hz is None:
        return None
    return float(p["fmin"]) * 1e6 <= float(freq_hz) <= float(p["fmax"]) * 1e6


def resolve(use, freq_hz=None, d=None):
    """Welche Antenne gilt für use ("scan"/"live") auf freq_hz? Rückgabe dict(profile, auto, outside) –
    profile None, wenn keine angelegt ist oder bei „auto“ kein Bereich passt (und es mehrere gibt)."""
    d = d or load()
    profs = d.get("profiles") or []
    sel = (d.get("use") or {}).get(use, "auto")
    if sel != "auto":
        p = next((x for x in profs if x["id"] == sel), None)
        if p is not None:
            return {"profile": p, "auto": False, "outside": covers(p, freq_hz) is False}
    fit = [x for x in profs if covers(x, freq_hz)]
    if fit:
        fit.sort(key=lambda x: float(x["fmax"]) - float(x["fmin"]))
        return {"profile": fit[0], "auto": True, "outside": False}
    open_ = [x for x in profs if x.get("fmin") is None or x.get("fmax") is None]
    if len(open_) == 1:                                  # eine Antenne ohne Bereich: gilt für alles
        return {"profile": open_[0], "auto": True, "outside": False}
    if len(profs) == 1:                                  # nur eine Antenne: die hängt dran, auch außerhalb
        return {"profile": profs[0], "auto": True, "outside": freq_hz is not None}
    return {"profile": None, "auto": True, "outside": False}


def label(use, freq_hz=None):
    """Name der Antenne für use auf freq_hz (für Treffer, Überflüge, PSK Reporter) oder None."""
    try:
        p = resolve(use, freq_hz)["profile"]
        return p["name"] if p else None
    except Exception:
        return None


def beam(use, freq_hz=None):
    """Richtantenne für use auf freq_hz: dict(az, bw, el, fmin, fmax, name) oder None (Rundstrahler/unbekannt)."""
    try:
        p = resolve(use, freq_hz)["profile"]
    except Exception:
        return None
    if not p or p.get("az") is None:
        return None
    return {"az": float(p["az"]) % 360, "bw": p.get("bw"), "el": float(p.get("el") or 0.0),
            "fmin": p.get("fmin"), "fmax": p.get("fmax"), "name": p["name"]}


def select(use, pid, log=None):
    """Antenne für den Scan oder für Live wählen ("auto" = nach Band) – sofort wirksam. Die alten Ziele
    rtlsdr/pmsdr (bis 1.8.32) zählen als Scan."""
    use = {"rtlsdr": "scan", "pmsdr": "scan"}.get(use, use)
    if use not in USES:
        raise ValueError("Ziel: scan oder live")
    d = load()
    if pid != "auto":
        prof = next((p for p in d["profiles"] if p["id"] == pid), None)
        if prof is None:
            raise ValueError("Antennen-Profil nicht gefunden")
    else:
        prof = {"id": "auto", "name": "automatisch nach Band"}
    old = (d.get("use") or {}).get(use)
    old_name = next((p["name"] for p in d["profiles"] if p["id"] == old), old)
    d.setdefault("use", {})[use] = pid
    save(d)
    if log and old != pid:
        log("INFO", "antennas", "antenna_changed", target=use, old=old_name, new=prof["name"])
    return prof
