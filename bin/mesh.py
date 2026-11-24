#!/usr/bin/env python3
"""MeshCore-Knoten am USB mithören – nur empfangen (Schritt 1).

Ein LoRa-Knoten mit der Firmware „Companion Radio (USB)“ (z. B. Heltec V3/V4, RAK) steckt am Pi. Hesperos DX
spricht mit ihm über das Companion-Protokoll (seriell, 115200 Bd) und liest:
  • Nachrichten aus den Kanälen des Knotens (öffentlicher Kanal u. a.) und Direktnachrichten an den Knoten,
  • die Knoten, die er gehört hat (Name, Art, Position, letzte Ankündigung),
  • eine Zählung aller empfangenen Pakete (SNR/RSSI des letzten) – nur im Speicher.
Der Knoten dekodiert selbst; der Pi liest nur ein paar Bytes. Ins Mesh gesendet wird nichts.
Einstellen (Schritt 3, 1.8.48): Uhr stellt der Pi selbst (beim Verbinden und täglich, nur bei mehr als 10 s
Abweichung); Name, Position und #Kanäle nur auf Knopfdruck im Dashboard (command()); Funkdaten bleiben, wie sie sind.
Statistik (Akku, Laufzeit, Rauschen, Sendezeit) jede Minute aus dem Knoten, nur im Speicher.

Ohne Zusatzpakete (nur Standardbibliothek): Rahmen vom Pi an den Knoten beginnen mit „<“, vom Knoten mit „>“,
danach 2 Byte Länge (little endian) und die Nutzdaten; Aufbau wie in der MeshCore-Doku (Companion Radio Protocol).
Gespeichert wird in scanner.db (mesh_node, mesh_msg) – nur bei neuen Nachrichten und geänderten Knoten.
"""
import glob
import hashlib
import os
import queue
import select
import sqlite3
import termios
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "scanner.db"
# USB-Hersteller typischer LoRa-Knoten: Espressif (ESP32-S3 direkt), Silicon Labs CP210x, WCH CH340/CH9102,
# Adafruit/RAK (nRF52), Seeed, Nordic. Andere serielle Geräte (GPS, Funkgeräte) werden nie angesprochen.
VENDORS = {"303a", "10c4", "1a86", "239a", "2886", "1915"}
DEV_GLOB = os.environ.get("HSP_MESH_DEV_GLOB", "/dev/serial/by-id/*")
SYS_TTY = os.environ.get("HSP_MESH_SYS_TTY", "/sys/class/tty")
NODE_TYPES = {1: "Companion", 2: "Repeater", 3: "Room", 4: "Sensor"}
PAYLOAD_TYPES = ["REQ", "RESPONSE", "TEXT_MSG", "ACK", "ADVERT", "GRP_TXT", "GRP_DATA", "ANON_REQ", "PATH",
                 "TRACE", "MULTIPART", "CONTROL"]
KEEP_DAYS = 90                                   # Nachrichten älter als das werden einmal am Tag gelöscht

STATUS = {"state": "off", "port": "", "error": "", "since": None, "self": {}, "device": {}, "channels": {},
          "last_rx": None, "nodes_known": 0, "stats": {}, "clock": {}}
ROOMS = {}                                       # Schlüssel eines Room-Servers → Anmeldung (name, logged_in, pending …)
_CMDS = queue.Queue()                            # Aufträge aus dem Dashboard an den Lesefaden (command())
CLOCK_MAX_DRIFT = 10                             # s – darüber stellt der Pi die Uhr des Knotens
_RX = deque(maxlen=20000)                        # (Zeit, Pakettyp) der letzten Stunde
_LOCK = threading.Lock()
_started = [False]
# Rückruf fürs Dashboard (Beobachtungsliste): ON_EVENT[0](art, daten) – art „msg“ (neue Nachricht) oder „node“
# (Knoten hat sich gemeldet). Wird im Lesefaden gerufen und darf nicht blockieren.
ON_EVENT = [None]


def _emit(kind, data):
    cb = ON_EVENT[0]
    if cb:
        try:
            cb(kind, data)
        except Exception:
            pass


def _iso(t=None):
    return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).isoformat(timespec="seconds")


def ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS mesh_node (
        key TEXT PRIMARY KEY, name TEXT, type INTEGER, lat REAL, lon REAL, last_advert INTEGER,
        first_seen TEXT, last_seen TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mesh_msg (
        id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, sender_ts INTEGER, kind TEXT, channel INTEGER,
        channel_name TEXT, sender TEXT, text TEXT, snr REAL, hops INTEGER, UNIQUE(kind, channel, sender_ts, text))""")
    db.execute("CREATE INDEX IF NOT EXISTS mesh_msg_time ON mesh_msg(time)")


def _db():
    db = sqlite3.connect(DB, timeout=15)
    db.execute("PRAGMA busy_timeout=15000")
    return db


# ---------- Geräte finden ----------

def _vendor(tty):
    d = os.path.realpath(os.path.join(SYS_TTY, tty, "device"))
    for _ in range(5):                                       # ttyACM: eine Ebene, ttyUSB: zwei Ebenen darüber
        f = os.path.join(d, "idVendor")
        if os.path.exists(f):
            try:
                return Path(f).read_text().strip().lower()
            except OSError:
                return ""
        d = os.path.dirname(d)
    return ""


def candidates():
    """Serielle USB-Geräte, die ein LoRa-Knoten sein könnten: [(Pfad by-id, /dev/tty…, Hersteller)]."""
    out = []
    for p in sorted(glob.glob(DEV_GLOB)):
        real = os.path.realpath(p)
        vid = _vendor(os.path.basename(real))
        if vid in VENDORS:
            out.append((p, real, vid))
    return out


# ---------- Verbindung ----------

class NoAccess(Exception):
    pass


class Link:
    def __init__(self, path):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError as e:
            raise NoAccess(str(e))
        a = termios.tcgetattr(self.fd)
        a[0] = 0                                             # iflag: roh
        a[1] = 0                                             # oflag
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        a[3] = 0                                             # lflag
        a[4] = a[5] = termios.B115200
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.buf = b""

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def send(self, payload):
        os.write(self.fd, b"<" + len(payload).to_bytes(2, "little") + payload)

    def frame(self, timeout):
        """Nächster Rahmen vom Knoten oder None nach timeout s. OSError/EOFError = Verbindung weg."""
        end = time.time() + timeout
        while True:
            i = self.buf.find(b">")
            if i < 0:
                self.buf = b""
            elif len(self.buf) >= i + 3:
                n = int.from_bytes(self.buf[i + 1:i + 3], "little")
                if n == 0 or n > 300:                        # kaputter Kopf: ein Byte weiter suchen
                    self.buf = self.buf[i + 1:]
                    continue
                if len(self.buf) >= i + 3 + n:
                    f = self.buf[i + 3:i + 3 + n]
                    self.buf = self.buf[i + 3 + n:]
                    return f
                self.buf = self.buf[i:]
            left = end - time.time()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                d = os.read(self.fd, 4096)
                if not d:
                    raise EOFError("Gerät liefert nichts mehr")
                self.buf += d


# ---------- Rahmen lesen ----------

def _s8(b):
    return b - 256 if b > 127 else b


def _i32(b):
    return int.from_bytes(b, "little", signed=True)


def _u32(b):
    return int.from_bytes(b, "little")


def _str(b):
    return b.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def parse_self(f):
    return {"type": f[1], "tx_power": f[2], "max_tx_power": f[3], "key": f[4:36].hex(), "lat": _i32(f[36:40]) / 1e6, "lon": _i32(f[40:44]) / 1e6,
            "freq_mhz": _u32(f[48:52]) / 1000, "bw_khz": _u32(f[52:56]) / 1000, "sf": f[56], "cr": f[57],
            "name": f[58:].decode("utf-8", "replace").strip("\0 ")}


def parse_device(f):
    d = {"fw": f[1]}
    if f[1] >= 3 and len(f) >= 80:
        d.update(max_channels=f[3], build=_str(f[8:20]), model=_str(f[20:60]), version=_str(f[60:80]))
    return d


def parse_contact(f):
    lat, lon = _i32(f[136:140]) / 1e6, _i32(f[140:144]) / 1e6
    return {"key": f[1:33].hex(), "type": f[33], "name": _str(f[100:132]), "last_advert": _u32(f[132:136]),
            "lat": lat if (lat or lon) else None, "lon": lon if (lat or lon) else None}


def parse_msg(f):
    """Kanal- oder Direktnachricht (Antwort auf „nächste Nachricht“), Protokoll ab Version 3 mit SNR."""
    code, p = f[0], 1
    snr = None
    if code in (16, 17):
        snr, p = _s8(f[1]) / 4, 4
    if code in (7, 16):
        kind, chan, sender = "dm", None, f[p:p + 6].hex()
        p += 6
    else:
        kind, chan, sender = "chan", f[p], None
        p += 1
    plen = f[p]
    hops = None if plen == 255 else plen & 0x3F
    txt_type = f[p + 1]
    ts = _u32(f[p + 2:p + 6])
    p += 6
    author = None
    if kind == "dm" and txt_type == 2:                       # „signiert“: bei Room-Servern die ersten 4 Byte des Autors
        author = f[p:p + 4].hex()
        p += 4
    text = f[p:].rstrip(b"\0").decode("utf-8", "replace")
    if kind == "chan" and ": " in text[:40]:                 # Kanal: „Name: Text“
        sender, text = text.split(": ", 1)
    return {"kind": kind, "channel": chan if kind == "chan" else -1, "sender": sender, "text": text, "sender_ts": ts,
            "snr": snr, "hops": hops, "txt_type": txt_type, "author": author}


# ---------- Ablauf ----------

class Session:
    def __init__(self, link, log):
        self.l, self.log = link, log
        self.waiting = True
        self.contacts_mod = 0

    def drain(self):
        while True:                                          # Aufträge aus einer früheren Verbindung verwerfen
            try:
                _n, _k, box, done = _CMDS.get_nowait()
                box.update(ok=False, error="Verbindung war weg – bitte nochmal")
                done.set()
            except queue.Empty:
                break

    def request(self, payload, want, timeout=4):
        self.l.send(payload)
        end = time.time() + timeout
        while time.time() < end:
            f = self.l.frame(end - time.time())
            if f is None:
                break
            if f[0] in want:
                return f
            self.handle(f)
        return None

    def start(self):
        f = self.request(b"\x01\x03      hesperos", (5,), timeout=5)
        if not f:
            return False
        with _LOCK:
            STATUS["self"] = parse_self(f)
        d = self.request(b"\x16\x03", (13, 1))
        if d and d[0] == 13:
            STATUS["device"] = parse_device(d)
        self.read_channels()
        self.clock()
        self.stats()
        self.contacts()
        return True

    def read_channels(self):
        chans, n = {}, int(STATUS["device"].get("max_channels") or 16)
        for i in range(min(n, 40)):
            c = self.request(b"\x1f" + bytes([i]), (18, 1), timeout=2)
            if not c or c[0] != 18:
                break
            name = _str(c[2:34])
            if name:
                chans[i] = name
        STATUS["channels"] = chans
        STATUS["free_channel"] = next((i for i in range(1, min(n, 40)) if i not in chans), None)

    def clock(self):
        """Uhr des Knotens mit der des Pi vergleichen und bei mehr als CLOCK_MAX_DRIFT s stellen (der Pi hat NTP)."""
        f = self.request(b"\x05", (9, 1), timeout=3)
        if not f or f[0] != 9 or len(f) < 5:
            return
        drift = _u32(f[1:5]) - int(time.time())
        c = {"drift_s": drift, "checked": _iso()}
        if abs(drift) > CLOCK_MAX_DRIFT:
            r = self.request(b"\x06" + int(time.time()).to_bytes(4, "little"), (0, 1), timeout=3)
            c["set"] = bool(r and r[0] == 0)
            self.log("INFO" if c["set"] else "WARN", "mesh", "mesh_clock_set" if c["set"] else "mesh_clock_failed",
                     drift_s=drift)
        STATUS["clock"] = c

    def stats(self):
        """Akku, Laufzeit, Rauschen, Sende-/Empfangszeit und Paketzähler (ältere Firmware: kennt das nicht)."""
        if STATUS["stats"].get("unsupported"):
            return
        st = {}
        for sub in (0, 1, 2):
            f = self.request(bytes([56, sub]), (24, 1), timeout=3)
            if not f or f[0] != 24:
                if sub == 0:
                    STATUS["stats"] = {"unsupported": True}
                    return
                continue
            if sub == 0 and len(f) >= 11:
                st.update(battery_mv=int.from_bytes(f[2:4], "little"), uptime_s=_u32(f[4:8]))
            elif sub == 1 and len(f) >= 14:
                st.update(noise_floor=int.from_bytes(f[2:4], "little", signed=True), tx_air_s=_u32(f[6:10]),
                          rx_air_s=_u32(f[10:14]))
            elif sub == 2 and len(f) >= 26:
                st.update(recv=_u32(f[2:6]), sent=_u32(f[6:10]))
        st["time"] = _iso()
        STATUS["stats"] = st

    def room_logins(self):
        """Am eigenen Room-Server (am USB, Text-Konsole) anmelden, damit er seine Beiträge an diesen Knoten schickt –
        mit dem Gast-Passwort aus seiner Konsole (wird nie angezeigt). Beim ersten Mal, nach einem Neustart des
        Servers und alle 6 h; kennt der Companion den Server noch nicht, sendet der Server eine Ankündigung."""
        for port, sv in list(SERVERS.items()):
            key = sv.get("_key")
            if sv.get("role_id") != "room_server" or not key:
                continue
            r = ROOMS.setdefault(key, {"name": sv.get("name"), "own": True})
            r["name"] = sv.get("name")
            fresh = r.get("logged_in") and r.get("server_since") == sv.get("since") and time.time() - r.get("tried", 0) < 6 * 3600
            if fresh or r.get("pending") or time.time() - r.get("tried", 0) < 120:
                continue
            r.update(tried=time.time(), server_since=sv.get("since"))
            res = self.request(b"\x1a" + bytes.fromhex(key) + (sv.get("_pw") or "").encode("utf-8"), (6, 1), timeout=4)
            if res and res[0] == 6:
                r.update(pending=True, failed=False)
            else:                                            # Kontakt fehlt: Server soll sich ankündigen
                r.update(pending=False, failed=False, no_contact=True, logged_in=None)
                sv["_advert"] = True

    def refresh_self(self):
        f = self.request(b"\x01\x03      hesperos", (5,), timeout=5)
        if f:
            with _LOCK:
                STATUS["self"] = parse_self(f)

    def run_command(self, name, kw):
        """Ein Auftrag aus dem Dashboard. Rückgabe dict(ok, error?)."""
        ok = lambda r: bool(r and r[0] == 0)
        if name == "name":
            n = str(kw.get("name", "")).strip()
            if not 0 < len(n.encode("utf-8")) <= 31:
                return {"ok": False, "error": "Name: 1 bis 31 Zeichen"}
            r = self.request(b"\x08" + n.encode("utf-8"), (0, 1))
            self.refresh_self()
            return {"ok": ok(r)}
        if name == "position":
            lat, lon = kw.get("lat"), kw.get("lon")
            if lat is None or lon is None:
                lat = lon = 0.0                              # 0/0 = keine Position
            r = self.request(b"\x0e" + int(float(lat) * 1e6).to_bytes(4, "little", signed=True)
                             + int(float(lon) * 1e6).to_bytes(4, "little", signed=True) + bytes(4), (0, 1))
            self.refresh_self()
            return {"ok": ok(r)}
        if name == "channel_add":
            n = str(kw.get("name", "")).strip()
            if not n.startswith("#"):
                n = "#" + n
            if not 1 < len(n.encode("utf-8")) <= 31 or " " in n:
                return {"ok": False, "error": "Kanal: #name ohne Leerzeichen, höchstens 30 Zeichen"}
            if n.lower() in (v.lower() for v in STATUS["channels"].values()):
                return {"ok": False, "error": "Kanal ist schon eingetragen"}
            idx = STATUS.get("free_channel")
            if idx is None:
                return {"ok": False, "error": "Kein Kanalplatz mehr frei"}
            secret = hashlib.sha256(n.encode("utf-8")).digest()[:16]   # öffentlicher #Kanal: Schlüssel aus dem Namen
            r = self.request(b"\x20" + bytes([idx]) + n.encode("utf-8").ljust(32, b"\0") + secret, (0, 1))
            self.read_channels()
            return {"ok": ok(r), "index": idx}
        if name == "channel_delete":
            idx = int(kw.get("index", -1))
            if idx <= 0 or idx not in STATUS["channels"]:
                return {"ok": False, "error": "Diesen Kanal gibt es nicht (Public bleibt)"}
            r = self.request(b"\x20" + bytes([idx]) + bytes(32) + bytes(16), (0, 1))
            self.read_channels()
            return {"ok": ok(r)}
        return {"ok": False, "error": "unbekannter Auftrag"}

    def contacts(self):
        self.l.send(b"\x04" + (self.contacts_mod.to_bytes(4, "little") if self.contacts_mod else b""))
        got, end = [], time.time() + 15
        while time.time() < end:
            f = self.l.frame(end - time.time())
            if f is None:
                break
            if f[0] == 3:
                got.append(parse_contact(f))
            elif f[0] == 4:
                self.contacts_mod = _u32(f[1:5]) or self.contacts_mod
                break
            elif f[0] == 1:
                break
            elif f[0] != 2:
                self.handle(f)
        store_nodes(got)

    def sync(self):
        for _ in range(200):
            f = self.request(b"\x0a", (7, 8, 16, 17, 10, 1), timeout=4)
            if not f or f[0] in (10, 1):
                break
            m = parse_msg(f)
            m["channel_name"] = STATUS["channels"].get(m["channel"], "") if m["kind"] == "chan" else ""
            if m["kind"] == "dm":
                room = _node_by_prefix(m["sender"])
                if room and room["type"] == 3 and m["author"]:   # Beitrag vom schwarzen Brett eines Room-Servers
                    a = _node_by_prefix(m["author"])
                    m.update(kind="room", channel=-2, channel_name=room["name"] or m["sender"],
                             sender=(a["name"] if a and a["name"] else "?" + m["author"]))
            if store_msg(m):
                _emit("msg", dict(m, sender=_dm_name(m["sender"]) if m["kind"] == "dm" else m["sender"]))
                self.log("DEBUG", "mesh", "mesh_msg", kind=m["kind"], channel=m["channel_name"] or m["channel"],
                         sender=m["sender"], hops=m["hops"])
        self.waiting = False

    def handle(self, f):
        c = f[0]
        if c == 0x83:                                        # Nachrichten warten
            self.waiting = True
        elif c in (0x85, 0x86):                              # Anmeldung an einem Room-Server: ja / nein
            pre = f[2:8].hex() if len(f) >= 8 else ""
            for k, r in ROOMS.items():
                if k.startswith(pre) and r.get("pending"):
                    r.update(pending=False, logged_in=_iso() if c == 0x85 else None, failed=c == 0x86,
                             admin=bool(c == 0x85 and len(f) > 1 and f[1] & 1), tried=time.time())
                    self.log("INFO" if c == 0x85 else "WARN", "mesh", "mesh_room_login" if c == 0x85 else "mesh_room_login_failed",
                             room=r.get("name"))
        elif c == 0x8A:                                      # neue Ankündigung (ganzer Kontakt)
            store_nodes([parse_contact(f)])
        elif c == 0x80:                                      # bekannter Knoten hat sich gemeldet
            store_seen(f[1:33].hex())
        elif c == 0x88 and len(f) >= 4:                      # jedes empfangene Paket (nur gezählt)
            t = time.time()
            pt = (f[3] & 0x3C) >> 2
            _RX.append((t, pt))
            with _LOCK:
                STATUS["last_rx"] = {"time": _iso(t), "snr": _s8(f[1]) / 4, "rssi": _s8(f[2]),
                                     "type": PAYLOAD_TYPES[pt] if pt < len(PAYLOAD_TYPES) else str(pt)}

    def loop(self, stop):
        t_sync, t_contacts, t_stats, t_clock = 0, time.time(), time.time(), time.time()
        while not stop():
            try:
                name, kw, box, done = _CMDS.get_nowait()
                try:
                    box.update(self.run_command(name, kw))
                except (OSError, EOFError):
                    box.update(ok=False, error="Verbindung weg")
                    raise
                finally:
                    done.set()
            except queue.Empty:
                pass
            if self.waiting or time.time() - t_sync > 60:
                self.sync()
                t_sync = time.time()
            if time.time() - t_stats > 60:
                self.stats()
                t_stats = time.time()
            self.room_logins()                               # eigener Room-Server: anmelden (gedrosselt)
            if time.time() - t_clock > 86400:
                self.clock()
                t_clock = time.time()
            if time.time() - t_contacts > 600:
                self.contacts()
                t_contacts = time.time()
            f = self.l.frame(1.0)
            if f is not None:
                self.handle(f)


# ---------- Speichern ----------

def store_msg(m):
    db = _db()
    try:
        ensure_tables(db)
        cur = db.execute("""INSERT OR IGNORE INTO mesh_msg (time, sender_ts, kind, channel, channel_name, sender, text, snr, hops)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (_iso(), m["sender_ts"], m["kind"], m["channel"], m.get("channel_name", ""), m["sender"],
                          m["text"], m["snr"], m["hops"]))
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def store_nodes(nodes):
    """Knoten eintragen – geschrieben wird nur, was neu ist oder sich geändert hat (SD-Karte schonen)."""
    if not nodes:
        return
    db = _db()
    try:
        ensure_tables(db)
        now, changed, heard = _iso(), False, []
        for n in nodes:
            r = db.execute("SELECT name, type, lat, lon, last_advert FROM mesh_node WHERE key=?", (n["key"],)).fetchone()
            new = (n["name"], n["type"], n["lat"], n["lon"], n["last_advert"])
            if r is None:
                db.execute("INSERT INTO mesh_node VALUES (?,?,?,?,?,?,?,?)", (n["key"], *new, now, now))
                changed = True
            elif tuple(r) != new:
                db.execute("UPDATE mesh_node SET name=?, type=?, lat=?, lon=?, last_advert=?, last_seen=? WHERE key=?",
                           (*new, now, n["key"]))
                changed = True
            else:
                continue
            # neu oder neue Ankündigung = „hat sich gemeldet“ – nur frische (sonst meldete der erste Abgleich nach
            # dem Anstecken alle Knoten, die der Knoten seit Wochen kennt)
            if (r is None or r[4] != n["last_advert"]) and n["last_advert"] > time.time() - 900:
                heard.append(n)
        if changed:
            db.commit()
        STATUS["nodes_known"] = db.execute("SELECT COUNT(*) FROM mesh_node").fetchone()[0]
    finally:
        db.close()
    for n in heard:
        _emit("node", _node_out(n))


_seen = {}


def store_seen(key):
    """„Hat sich gemeldet“ höchstens alle 10 min je Knoten schreiben (und melden)."""
    if time.time() - _seen.get(key, 0) < 600:
        return
    _seen[key] = time.time()
    db = _db()
    try:
        ensure_tables(db)
        db.execute("UPDATE mesh_node SET last_seen=? WHERE key=?", (_iso(), key))
        db.commit()
        r = db.execute("SELECT key, name, type, lat, lon, last_advert FROM mesh_node WHERE key=?", (key,)).fetchone()
    finally:
        db.close()
    if r:
        _emit("node", _node_out(dict(zip(("key", "name", "type", "lat", "lon", "last_advert"), r))))


def _node_out(n):
    return {"key": n["key"][:12], "name": n.get("name") or "", "type": NODE_TYPES.get(n.get("type"), str(n.get("type"))),
            "lat": n.get("lat"), "lon": n.get("lon")}


def _node_by_prefix(prefix):
    """Knoten aus der Liste über die ersten Byte seines Schlüssels (6 bei Direktnachrichten, 4 bei Room-Beiträgen)."""
    if not prefix:
        return None
    db = _db()
    try:
        ensure_tables(db)
        r = db.execute("SELECT key, name, type FROM mesh_node WHERE key LIKE ? LIMIT 1", (prefix.lower() + "%",)).fetchone()
    finally:
        db.close()
    return {"key": r[0], "name": r[1], "type": r[2]} if r else None


def _dm_name(prefix):
    """Absender einer Direktnachricht: Name aus der Knotenliste statt der ersten 6 Byte des Schlüssels."""
    if not prefix:
        return prefix
    db = _db()
    try:
        ensure_tables(db)
        r = db.execute("SELECT name FROM mesh_node WHERE substr(key,1,12)=?", (prefix,)).fetchone()
    finally:
        db.close()
    return r[0] if r and r[0] else prefix


def prune():
    db = _db()
    try:
        ensure_tables(db)
        db.execute("DELETE FROM mesh_msg WHERE time < ?", (_iso(time.time() - KEEP_DAYS * 86400),))
        db.commit()
    finally:
        db.close()


# ---------- Abfragen fürs Dashboard ----------

def status():
    t = time.time()
    with _LOCK:
        s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in STATUS.items()}
    recent = [p for x, p in list(_RX) if t - x < 3600]
    s["packets_hour"] = len(recent)
    s["packet_types"] = {PAYLOAD_TYPES[p] if p < len(PAYLOAD_TYPES) else str(p): recent.count(p) for p in set(recent)}
    s["channels"] = {str(k): v for k, v in (s.get("channels") or {}).items()}
    s["candidates"] = [p for p, _, _ in candidates()]
    s["servers"] = [{k: v for k, v in sv.items() if not k.startswith("_")} for sv in list(SERVERS.values())]
    s["rooms"] = [dict(r, key=k[:12]) for k, r in list(ROOMS.items())]
    return s


def messages(limit=100, before=None, kind=None, not_kind=None):
    db = _db()
    try:
        ensure_tables(db)
        q, a, w = "SELECT id, time, kind, channel, channel_name, sender, text, snr, hops FROM mesh_msg", [], []
        if before:
            w.append("id < ?")
            a.append(int(before))
        if kind:
            w.append("kind = ?")
            a.append(kind)
        if not_kind:
            w.append("kind != ?")
            a.append(not_kind)
        if w:
            q += " WHERE " + " AND ".join(w)
        rows = db.execute(q + " ORDER BY id DESC LIMIT ?", a + [int(limit)]).fetchall()
        tw = (" WHERE kind = ?", [kind]) if kind else (" WHERE kind != ?", [not_kind]) if not_kind else ("", [])
        total = db.execute("SELECT COUNT(*) FROM mesh_msg" + tw[0], tw[1]).fetchone()[0]
        names = dict(db.execute("SELECT substr(key,1,12), name FROM mesh_node"))
    finally:
        db.close()
    out = []
    for i, t, kind, ch, chn, sender, text, snr, hops in rows:
        if kind == "dm" and sender:
            sender = names.get(sender, sender)               # Direktnachricht: Name aus der Knotenliste
        out.append({"id": i, "time": t, "kind": kind, "channel": ch, "channel_name": chn, "sender": sender,
                    "text": text, "snr": snr, "hops": hops})
    return {"messages": out, "total": total}


def nodes():
    db = _db()
    try:
        ensure_tables(db)
        rows = db.execute("""SELECT key, name, type, lat, lon, last_advert, first_seen, last_seen FROM mesh_node
                             ORDER BY last_seen DESC LIMIT 1000""").fetchall()
    finally:
        db.close()
    return [{"key": k[:12], "name": n, "type": NODE_TYPES.get(t, str(t)), "lat": la, "lon": lo,
             "last_advert": _iso(adv) if adv else None, "first_seen": fs, "last_seen": ls}
            for k, n, t, la, lo, adv, fs, ls in rows]


# ---------- Hintergrund ----------

# ---------- Repeater, Room-Server, Sensor am USB (Text-Konsole, 1.8.49) ----------
# Diese Firmwares sprechen kein Companion-Protokoll, sondern eine Text-Konsole (115200 Bd): Befehl + „\r“, das Gerät
# wiederholt die Zeichen und antwortet mit „  -> Antwort“. Gelesen wird nur (ver, get …, stats-*, clock); geschrieben
# wird allein die Uhr (time <epoch>, nur vorwärts – so will es die Firmware).
SERVERS = {}                                     # Anschluss → Zustand eines Repeaters/Room-Servers am USB
_SCMDS = {}                                      # Anschluss → Auftragsschlange (Verwaltung aus dem Dashboard)
ROLES = {"room_server": "Room-Server", "repeater": "Repeater", "sensor": "Sensor"}


class CliSession:
    def __init__(self, link, log, port):
        self.l, self.log, self.port = link, log, port
        self.buf = b""

    def _read(self, timeout):
        r, _, _ = select.select([self.l.fd], [], [], timeout)
        if r:
            d = os.read(self.l.fd, 4096)
            if not d:
                raise EOFError("Gerät liefert nichts mehr")
            self.buf += d

    def cmd(self, text, timeout=3.0):
        """Befehl schicken, Antwort hinter „  -> “ zurückgeben (None = keine Antwort)."""
        self.buf = b""
        os.write(self.l.fd, text.encode("utf-8") + b"\r")
        end = time.time() + timeout
        while time.time() < end:
            self._read(max(0.05, end - time.time()))
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                t = line.decode("utf-8", "replace")
                if "  -> " in t:
                    return t.rsplit("  -> ", 1)[1].strip("\r ")
        return None

    def get(self, key):
        r = self.cmd("get " + key)
        return r[2:].strip() if r and r.startswith("> ") else None

    def start(self):
        self.cmd("", timeout=1.0)                            # Reste vom Companion-Versuch abschließen
        ver = self.cmd("ver")
        if not ver or "Build" not in ver and not ver.startswith("v"):
            return False
        role = self.get("role") or ""
        radio = (self.get("radio") or "").split(",")
        st = {"port": self.port, "role": ROLES.get(role, role or "?"), "role_id": role, "version": ver,
              "board": self.cmd("board") or "", "name": self.get("name") or "", "since": _iso(), "stats": {}, "clock": {}}
        try:
            st.update(freq_mhz=float(radio[0]), bw_khz=float(radio[1]), sf=int(radio[2]), cr=int(radio[3]))
        except (ValueError, IndexError):
            pass
        try:
            st["tx_power"] = int(self.get("tx") or "")
        except ValueError:
            pass
        try:
            la, lo = float(self.get("lat") or 0), float(self.get("lon") or 0)
            st.update(lat=la if (la or lo) else None, lon=lo if (la or lo) else None)
        except ValueError:
            pass
        SERVERS[self.port] = st
        self.read_admin()
        self.clock()
        self.stats()
        return True

    def read_admin(self):
        """Einstellungen für die Verwaltung. Das Gast-Passwort bleibt intern (_pw, für die Anmeldung des Companion)
        – angezeigt wird nur, ob eines gesetzt ist."""
        st = SERVERS[self.port]
        key = self.get("public.key") or ""
        st["_key"] = key.lower() if len(key) == 64 else ""
        st["key"] = st["_key"][:12]
        pw = self.get("guest.password")
        st["_pw"] = pw or ""
        st["guest_password_set"] = bool(pw)
        st["read_only"] = (self.get("allow.read.only") or "") == "on"
        st["owner_info"] = (self.get("owner.info") or "").replace("|", "\n")
        st["name"] = self.get("name") or st.get("name", "")
        try:
            la, lo = float(self.get("lat") or 0), float(self.get("lon") or 0)
            st.update(lat=la if (la or lo) else None, lon=lo if (la or lo) else None)
        except ValueError:
            pass

    def raw_lines(self, text, wait=1.5):
        """Befehl, dessen Ausgabe als eigene Zeilen kommt (get acl) – alles einsammeln, was in wait s eintrifft."""
        self.buf = b""
        os.write(self.l.fd, text.encode("utf-8") + b"\r")
        end = time.time() + wait
        while time.time() < end:
            self._read(max(0.05, end - time.time()))
        return [x.decode("utf-8", "replace").strip("\r ") for x in self.buf.split(b"\n")]

    def acl(self):
        perm = {0: "Gast (nur lesen)", 1: "Admin", 2: "Lesen und schreiben", 3: "Admin"}
        out = []
        for line in self.raw_lines("get acl"):
            m = __import__("re").match(r"^([0-9A-Fa-f]{2}) ([0-9A-Fa-f]{64})$", line)
            if m:
                p, k = int(m[1], 16), m[2].lower()
                n = _node_by_prefix(k[:12])
                me = (STATUS.get("self") or {}).get("key", "")[:12] == k[:12]
                out.append({"key": k[:12], "name": n["name"] if n else "", "me": me, "perm": perm.get(p & 3, str(p)),
                            "admin": bool(p & 1)})
        return out

    def run_command(self, name, kw):
        ok = lambda r: bool(r) and (r.startswith("OK") or r.startswith("> "))
        if name == "name":
            n = str(kw.get("name", "")).strip()
            if not 0 < len(n.encode("utf-8")) <= 31:
                return {"ok": False, "error": "Name: 1 bis 31 Zeichen"}
            r = self.cmd("set name " + n)
        elif name == "position":
            lat, lon = kw.get("lat"), kw.get("lon")
            r1 = self.cmd(f"set lat {0 if lat is None else round(float(lat), 5)}")
            r = self.cmd(f"set lon {0 if lon is None else round(float(lon), 5)}") if ok(r1) else r1
        elif name == "owner_info":
            t = str(kw.get("text", "")).replace("\n", "|").strip()
            if len(t) > 119:
                return {"ok": False, "error": "Höchstens 119 Zeichen"}
            r = self.cmd("set owner.info " + t)
        elif name == "guest_password":
            pw = str(kw.get("password", ""))
            if len(pw) > 15 or " " in pw:
                return {"ok": False, "error": "Gast-Passwort: höchstens 15 Zeichen, ohne Leerzeichen"}
            r = self.cmd("set guest.password " + pw)
        elif name == "read_only":
            r = self.cmd("set allow.read.only " + ("on" if kw.get("on") else "off"))
        elif name == "advert":
            r = self.cmd("advert")
        elif name == "post":
            t = str(kw.get("text", "")).strip()
            if not 0 < len(t.encode("utf-8")) <= 140:
                return {"ok": False, "error": "Beitrag: 1 bis 140 Zeichen"}
            r = self.cmd("room.post " + t)
            if r and r.startswith("Unknown"):
                return {"ok": False, "error": "Diese Firmware kann noch keine Beiträge vom Server (room.post) – MeshCore aktualisieren"}
        elif name == "acl":
            return {"ok": True, "acl": self.acl()}
        elif name == "reboot":
            os.write(self.l.fd, b"reboot\r")                 # antwortet nicht; die Verbindung kommt danach neu
            return {"ok": True}
        else:
            return {"ok": False, "error": "unbekannter Auftrag"}
        if name in ("name", "position", "owner_info", "guest_password", "read_only"):
            self.read_admin()
            for k, rr in ROOMS.items():                       # neues Passwort/Name: Companion meldet sich neu an
                if k == SERVERS[self.port].get("_key"):
                    rr.update(tried=0, logged_in=None)
        if ok(r) or (name == "advert" and r is not None and not r.startswith(("ERR", "Err", "Unknown"))):
            return {"ok": True, "reply": (r or "")[:160]}
        return {"ok": False, "error": (r or "keine Antwort")[:160]}

    def clock(self):
        """Uhr lesen („21:28 - 24/11/2026 UTC“, minutengenau) und stellen, wenn sie mehr als 90 s nachgeht."""
        import calendar
        r = self.cmd("clock") or ""
        try:
            hm, dmy = r.replace(" UTC", "").split(" - ")
            hh, mm = map(int, hm.split(":"))
            d, mo, y = map(int, dmy.split("/"))
            t = calendar.timegm((y, mo, d, hh, mm, 0))
        except ValueError:
            return
        drift = t - int(time.time())                         # minutengenau: ±60 s sind „stimmt“
        c = {"drift_s": drift, "checked": _iso()}
        if drift < -90:
            res = self.cmd(f"time {int(time.time())}") or ""
            c["set"] = res.startswith("OK")
            self.log("INFO" if c["set"] else "WARN", "mesh", "mesh_clock_set" if c["set"] else "mesh_clock_failed",
                     drift_s=drift, device=SERVERS[self.port].get("name"))
        elif drift > 90:
            c["ahead"] = True                                # vorgehende Uhr lässt die Firmware nicht zurückstellen
        SERVERS[self.port]["clock"] = c

    def stats(self):
        import json as _j
        st = {}
        for c in ("stats-core", "stats-radio", "stats-packets"):
            r = self.cmd(c)
            try:
                st.update(_j.loads(r))
            except (TypeError, ValueError):
                pass
        if st:
            st["time"] = _iso()
            SERVERS[self.port]["stats"] = st

    def loop(self, stop):
        t_stats, t_clock = time.time(), time.time()
        q = _SCMDS.setdefault(self.port, queue.Queue())
        while not stop():
            try:
                name, kw, box, done = q.get_nowait()
                try:
                    box.update(self.run_command(name, kw))
                finally:
                    done.set()
            except queue.Empty:
                pass
            if SERVERS[self.port].pop("_advert", False):     # der Companion kennt diesen Server noch nicht
                self.cmd("advert")
            self._read(1.0)                                  # Ausgaben des Geräts zwischendurch verwerfen
            self.buf = self.buf[-4096:]
            if time.time() - t_stats > 60:
                self.stats()
                t_stats = time.time()
            if time.time() - t_clock > 3600:                 # ohne Akku-Uhr: nach jedem Neustart wieder stellen
                self.clock()
                t_clock = time.time()


# ---------- Hintergrund ----------

_ACTIVE = {}                                     # Anschluss → "probing" | "companion" | "server"


def _worker(p, log, notice, stop, failed):
    try:
        link = Link(p)
    except NoAccess as e:
        failed[p] = True
        STATUS.update(state="error", error="no_access", port=p)
        log("WARN", "mesh", "mesh_no_access", port=p, error=str(e))
        if notice:
            notice("access", p)
        _ACTIVE.pop(p, None)
        return
    except OSError as e:
        failed[p] = True
        log("DEBUG", "mesh", "mesh_open_failed", port=p, error=str(e))
        _ACTIVE.pop(p, None)
        return
    kind = None
    try:
        time.sleep(1.5)                                      # ESP32 startet beim Öffnen manchmal neu
        if "companion" not in _ACTIVE.values():
            s = Session(link, log)
            if s.start():
                s.drain()
                kind = _ACTIVE[p] = "companion"
                me, dev = STATUS["self"], STATUS["device"]
                STATUS.update(state="connected", port=p, error="", since=_iso())
                log("INFO", "mesh", "mesh_connected", port=p, node=me.get("name"), model=dev.get("model"),
                    version=dev.get("version"), freq_mhz=me.get("freq_mhz"), sf=me.get("sf"), bw_khz=me.get("bw_khz"))
                if notice:
                    notice("ok", p)
                s.loop(lambda: stop(p))
                log("INFO", "mesh", "mesh_stopped", port=p)
                return
        cli = CliSession(link, log, p)
        if cli.start():
            kind = _ACTIVE[p] = "server"
            sv = SERVERS[p]
            log("INFO", "mesh", "mesh_server_connected", port=p, role=sv["role"], node=sv["name"], version=sv["version"],
                board=sv["board"])
            if notice:
                notice("ok", p)
            cli.loop(lambda: stop(p))
            log("INFO", "mesh", "mesh_stopped", port=p)
            return
        failed[p] = True
        log("INFO", "mesh", "mesh_no_answer", port=p, hint="keine MeshCore-Firmware mit Companion (USB) oder Text-Konsole?")
    except (OSError, EOFError) as e:
        log("WARN", "mesh", "mesh_lost", port=p, error=f"{type(e).__name__}: {e}")
        if kind == "companion":
            STATUS.update(error=str(e))
    finally:
        link.close()
        _ACTIVE.pop(p, None)
        SERVERS.pop(p, None)
        _SCMDS.pop(p, None)
        if kind == "companion":
            STATUS.update(state="searching")


def run(get_port, log, notice=None, stop=lambda: False, rescan=30):
    """Dauerschleife (eigener Faden im Dashboard): Geräte suchen und je Gerät einen Faden starten – ein Companion
    (Nachrichten, Knoten, Einstellungen) und beliebig viele Repeater/Room-Server (Zustand, Uhr). Ein Gerät, das gar
    nicht antwortet, wird erst nach dem Abziehen wieder versucht. get_port() → "auto" | "off" | Pfad."""
    failed, last_prune = {}, 0
    while not stop():
        port = (get_port() or "auto").strip()
        if time.time() - last_prune > 86400:
            try:
                prune()
            except Exception:
                pass
            last_prune = time.time()
        if port == "off":
            if not _ACTIVE:
                STATUS.update(state="off", port="", error="")
            time.sleep(min(rescan, 5))
            continue
        cands = [(port, os.path.realpath(port), "")] if port != "auto" else candidates()
        present = {p for p, _, _ in cands}
        for p in list(failed):
            if p not in present:
                failed.pop(p)                                # abgezogen: beim nächsten Anstecken wieder versuchen
        for p, _r, _v in cands:
            if p in failed or p in _ACTIVE:
                continue
            _ACTIVE[p] = "probing"
            keep = lambda q, port_now=port: (stop() or (get_port() or "auto").strip() not in ("auto", q))
            threading.Thread(target=_worker, args=(p, log, notice, keep, failed), daemon=True, name="mesh-" + p[-12:]).start()
        if "companion" not in _ACTIVE.values() and "probing" not in _ACTIVE.values() and STATUS.get("state") != "error":
            STATUS.update(state="searching", port="")
        time.sleep(2 if any(v == "probing" for v in _ACTIVE.values()) else min(rescan, 10))


def server_command(key, cmd, timeout=15, **kw):
    """Auftrag an einen Repeater/Room-Server am USB (key = die ersten 12 Zeichen seines Schlüssels)."""
    port = next((p for p, sv in SERVERS.items() if sv.get("key") == key), None)
    if not port or port not in _SCMDS:
        return {"ok": False, "error": "Server nicht verbunden"}
    box, done = {}, threading.Event()
    _SCMDS[port].put((cmd, kw, box, done))
    if not done.wait(timeout):
        return {"ok": False, "error": "Server antwortet nicht"}
    return box


def command(cmd, timeout=15, **kw):
    """Auftrag ans verbundene Gerät (aus dem Dashboard): wartet auf das Ergebnis. dict(ok, error?, …)."""
    if STATUS.get("state") != "connected":
        return {"ok": False, "error": "Kein Knoten verbunden"}
    box, done = {}, threading.Event()
    _CMDS.put((cmd, kw, box, done))
    if not done.wait(timeout):
        return {"ok": False, "error": "Knoten antwortet nicht"}
    return box


def start(get_port, log, notice=None):
    if _started[0]:
        return
    _started[0] = True
    threading.Thread(target=run, args=(get_port, log, notice), daemon=True, name="mesh").start()
