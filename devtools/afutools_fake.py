#!/usr/bin/env python3
"""Nachgebildete afu.tools-API für den Selbsttest (1.8.63): /api/v1/repeaters aus afutools_fixture.json
(Ausschnitt einer echten Antwort), nur mit X-API-Key „afu_test“; sonst 401 wie das Original.
Aufruf: afutools_fake.py <port>   – merkt sich die Abfragen in <port>.log neben der Datei (für den Test)."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

FIX = json.loads((Path(__file__).parent / "afutools_fixture.json").read_text(encoding="utf-8"))
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
        return self._send(404, {"error": "nicht gefunden", "status": 404})

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
