#!/usr/bin/env python3
"""Nachgebildete afu.tools-API für den Selbsttest (1.8.63): /api/v1/repeaters aus afutools_fixture.json
(Ausschnitt einer echten Antwort), /api/v1/qth aus afutools_fixture_qth.json (Quellenangabe echt, Rufzeichen erfunden), nur mit X-API-Key „afu_test“; sonst 401 wie das Original.
Aufruf: afutools_fake.py <port>   – merkt sich die Abfragen in <port>.log neben der Datei (für den Test)."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

FIX = json.loads((Path(__file__).parent / "afutools_fixture.json").read_text(encoding="utf-8"))
FIXQ = json.loads((Path(__file__).parent / "afutools_fixture_qth.json").read_text(encoding="utf-8"))
FIXW = json.loads((Path(__file__).parent / "afutools_fixture_wx.json").read_text(encoding="utf-8"))
FIXC = json.loads((Path(__file__).parent / "afutools_fixture_cal.json").read_text(encoding="utf-8"))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
LOG = Path(sys.argv[2]) if len(sys.argv) > 2 else None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if LOG:
            with LOG.open("a") as f:
                f.write(json.dumps({"path": u.path, "qs": parse_qs(u.query), "key": self.headers.get("X-API-Key")}) + "\n")
        if self.headers.get("X-API-Key") != "afu_test":
            return self._send(401, {"error": "Schlüssel fehlt oder ungültig", "status": 401})
        if u.path == "/api/v1/repeaters":
            return self._send(200, FIX)
        if u.path == "/api/v1/contests":
            return self._send(200, FIXC["contests"])
        if u.path == "/api/v1/dx":
            return self._send(200, FIXC["dx"])
        if u.path == "/api/v1/funkwetter":
            return self._send(200, FIXW["funkwetter"])
        if u.path == "/api/v1/funkwetter/offen":
            return self._send(200, FIXW["offen"])
        if u.path == "/api/v1/qth":                  # nur die gefragten Rufzeichen, die die Vorlage kennt
            rufe = [c.strip().upper() for c in (parse_qs(u.query).get("rufe") or [""])[0].split(",") if c.strip()]
            if len(rufe) > 100:
                return self._send(422, {"error": "höchstens 100 Rufzeichen", "status": 422})
            return self._send(200, {"quellenangabe": FIXQ["quellenangabe"], "qth": {c: FIXQ["qth"][c] for c in rufe if c in FIXQ["qth"]}})
        return self._send(404, {"error": "nicht gefunden", "status": 404})

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
