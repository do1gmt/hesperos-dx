#!/usr/bin/env python3
"""Nachgebildeter MeshCore-Companion-Node (USB) für Selbsttest und UI-Probe – ohne Hardware.

    python3 devtools/mesh_fake.py <ordner> [room]   (room: Room-Server mit Text-Konsole statt Companion) legt <ordner>/by-id/usb-Espressif_fake-if00 (→ pty) und
                                                <ordner>/sys/<pts>/device/idVendor = 303a an und antwortet wie
                                                ein Node (Kanäle, Kontakte, Nachrichten, Paket-Log).
Danach: HSP_MESH_DEV_GLOB=<ordner>/by-id/* HSP_MESH_SYS_TTY=<ordner>/sys
"""
import os
import pty
import select
import sys
import time
import tty


def frame(p):
    return b">" + len(p).to_bytes(2, "little") + p


def contact(key, typ, name, adv, lat, lon):
    return (bytes([3]) + key + bytes([typ, 0, 255]) + bytes(64) + name.encode().ljust(32, b"\0")
            + adv.to_bytes(4, "little") + int(lat * 1e6).to_bytes(4, "little", signed=True)
            + int(lon * 1e6).to_bytes(4, "little", signed=True) + (1000).to_bytes(4, "little"))


def room(m):
    """Room-Server: Text-Konsole wie die MeshCore-Firmware (Zeichen zurück, Antwort mit „  -> “)."""
    clock = int(time.time()) - 3 * 3600                      # Uhr nach Neustart ohne Akku-Uhr: 3 h nach
    cfg = {"guest": "geheim", "ro": "off", "owner": "Tom, DO1GMT", "name": "DO1GMT Room", "lat": "49.123", "lon": "10.456"}
    t0, buf = time.time(), b""
    while True:
        r, _, _ = select.select([m], [], [], 0.5)
        if not r:
            continue
        try:
            d = os.read(m, 4096)
        except OSError:
            return
        for ch in d:
            c = bytes([ch])
            if c == b"\n":
                continue
            os.write(m, c)                                   # Echo wie die Firmware
            buf += c
            if c != b"\r":
                continue
            cmd, buf = buf[:-1].decode("latin-1"), b""
            now = clock + int(time.time() - t0)
            g = time.gmtime(now)
            rep = {"ver": "v1.18.0 (Build: 23-Oct-2026)", "board": "Heltec V4", "get role": "> room_server",
                   "get name": "> DO1GMT Room", "get radio": "> 869.618,62.5,8,5", "get tx": "> 22", "get lat": "> 49.123",
                   "get lon": "> 10.456", "clock": f"{g.tm_hour:02d}:{g.tm_min:02d} - {g.tm_mday}/{g.tm_mon}/{g.tm_year} UTC",
                   "stats-core": '{"battery_mv":4980,"uptime_secs":5400,"errors":0,"queue_len":0}',
                   "stats-radio": '{"noise_floor":-110,"last_rssi":-70,"last_snr":9.25,"tx_air_secs":33,"rx_air_secs":812}',
                   "stats-packets": '{"recv":2410,"sent":96,"flood_tx":80,"direct_tx":16,"flood_rx":2000,"direct_rx":410,"recv_errors":3}'}.get(cmd)
            if cmd == "get public.key":
                rep = "> " + "33" * 32
            elif cmd == "get guest.password":
                rep = "> " + cfg["guest"]
            elif cmd == "get allow.read.only":
                rep = "> " + cfg["ro"]
            elif cmd == "get owner.info":
                rep = "> " + cfg["owner"]
            elif cmd.startswith("set ") and " " in cmd[4:]:
                k, v = cmd[4:].split(" ", 1)
                cfg[{"guest.password": "guest", "allow.read.only": "ro", "owner.info": "owner", "name": "name",
                     "lat": "lat", "lon": "lon"}.get(k, k)] = v
                rep = "OK"
            elif cmd == "advert":
                rep = "OK - Advert sent"
            elif cmd.startswith("room.post "):
                rep = "OK"
            elif cmd == "get acl":
                os.write(m, b"ACL:\r\n03 " + ("11" * 32).encode() + b"\r\n02 " + ("aabbccddeeff" + "01" * 26).encode() + b"\r\n")
                continue
            elif cmd in ("get name", "get lat", "get lon"):
                rep = "> " + cfg[cmd[4:]]
            if cmd.startswith("time "):
                v = int(cmd[5:])
                if v > now:
                    clock, t0 = v, time.time()
                    rep = "OK - clock set"
                else:
                    rep = "(ERR: clock cannot go backwards)"
            if rep is None:
                rep = "Unknown command"
            os.write(m, ("  -> " + rep + "\r\n").encode())


def main(d, mode="companion"):
    os.makedirs(d + "/by-id", exist_ok=True)
    m, s = pty.openpty()
    tty.setraw(m)
    name = os.ttyname(s)
    link = d + ("/by-id/usb-Espressif_Systems_room_fake-if00" if mode == "room" else "/by-id/usb-Espressif_Systems_heltec_fake-if00")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(name, link)
    sd = f"{d}/sys/{os.path.basename(name)}/device"
    os.makedirs(sd, exist_ok=True)
    open(sd + "/idVendor", "w").write("303a\n")
    print(link, flush=True)
    if mode == "room":
        return room(m)
    now = int(time.time())
    queue = [  # Nachrichten, die „warten“
        bytes([17, 40, 0, 0, 0, 2, 0]) + now.to_bytes(4, "little") + "DL1ABC Mobil: Hallo Mesh aus Franken".encode(),
        bytes([16, 0xF4, 0, 0]) + bytes.fromhex("aabbccddeeff") + bytes([1, 0]) + (now - 5).to_bytes(4, "little") + "Direkt an dich".encode(),
    ]
    buf, pushed = b"", False
    me = {"name": "DO1GMT Pi", "lat": 49.8, "lon": 9.9}
    chans = {0: "Public", 1: "#franken"}
    offset = -300                                            # Uhr des Nodes geht 5 min nach
    t0 = time.time()
    while True:
        r, _, _ = select.select([m], [], [], 0.5)
        if not pushed and time.time() - t0 > 3:              # nach dem Start: Paket-Log, neue Ankündigung, Hinweis
            pushed = True
            os.write(m, frame(bytes([0x88, 28, 0xA6, (5 << 2) | 1, 0, 1, 2])))
            os.write(m, frame(bytes([0x8A]) + contact(b"\x22" * 32, 2, "DB0FRK Repeater", now, 49.79, 9.93)[1:]))
            queue.append(bytes([17, 20, 0, 0, 0, 1, 0]) + (now + 1).to_bytes(4, "little") + "Tom: zweite Nachricht".encode())
            os.write(m, frame(bytes([0x83])))
        if not r:
            continue
        try:
            buf += os.read(m, 4096)
        except OSError:
            return
        while len(buf) >= 3 and buf[0:1] == b"<":
            n = int.from_bytes(buf[1:3], "little")
            if len(buf) < 3 + n:
                break
            p, buf = buf[3:3 + n], buf[3 + n:]
            c = p[0]
            if c == 1:
                os.write(m, frame(bytes([5, 1, 22, 22]) + b"\x11" * 32 + int(me["lat"] * 1e6).to_bytes(4, "little", signed=True)
                                  + int(me["lon"] * 1e6).to_bytes(4, "little", signed=True) + bytes([0, 0, 0, 0])
                                  + (869525).to_bytes(4, "little") + (250000).to_bytes(4, "little") + bytes([11, 5])
                                  + me["name"].encode()))
            elif c == 0x16:
                os.write(m, frame(bytes([13, 9, 50, 4]) + bytes(4) + b"23 Oct 2026".ljust(12, b"\0")
                                  + b"Heltec V4".ljust(40, b"\0") + b"v1.9.1".ljust(20, b"\0") + bytes([0])))
            elif c == 0x1F:
                i = p[1]
                if i < 4:
                    os.write(m, frame(bytes([18, i]) + chans.get(i, "").encode().ljust(32, b"\0") + bytes(16)))
                else:
                    os.write(m, frame(bytes([1, 2])))
            elif c == 0x20:                                  # Kanal setzen / löschen (leerer Name)
                n = p[2:34].split(b"\0")[0].decode()
                if n:
                    chans[p[1]] = n
                else:
                    chans.pop(p[1], None)
                os.write(m, frame(bytes([0])))
            elif c == 0x05:
                os.write(m, frame(bytes([9]) + (int(time.time()) + offset).to_bytes(4, "little")))
            elif c == 0x06:
                offset = int.from_bytes(p[1:5], "little") - int(time.time())
                os.write(m, frame(bytes([0])))
            elif c == 0x08:
                me["name"] = p[1:].decode()
                os.write(m, frame(bytes([0])))
            elif c == 0x0E:
                me["lat"], me["lon"] = int.from_bytes(p[1:5], "little", signed=True) / 1e6, int.from_bytes(p[5:9], "little", signed=True) / 1e6
                os.write(m, frame(bytes([0])))
            elif c == 0x1A:                                  # Anmeldung an einem Room-Server
                key, pw = p[1:33], p[33:].decode()
                os.write(m, frame(bytes([6, 0]) + bytes(4) + (3000).to_bytes(4, "little")))
                if key == b"\x33" * 32 and pw == "geheim":
                    os.write(m, frame(bytes([0x85, 0]) + b"\x33" * 6 + bytes(6)))
                    queue.append(bytes([16, 30, 0, 0]) + b"\x33" * 6 + bytes([0, 2]) + int(time.time()).to_bytes(4, "little")
                                 + bytes.fromhex("aabbccdd") + "Schwarzes Brett: Treffen Samstag 10 Uhr".encode())
                    os.write(m, frame(bytes([0x83])))
                else:
                    os.write(m, frame(bytes([0x86, 0]) + key[:6]))
            elif c == 56:
                sub = p[1]
                body = {0: (4150).to_bytes(2, "little") + (7200).to_bytes(4, "little") + bytes(3),
                        1: (-112).to_bytes(2, "little", signed=True) + bytes([0xA6, 28]) + (12).to_bytes(4, "little") + (340).to_bytes(4, "little"),
                        2: b"".join(x.to_bytes(4, "little") for x in (500, 7, 3, 4, 400, 100))}[sub]
                os.write(m, frame(bytes([24, sub]) + body))
            elif c == 4:
                os.write(m, frame(bytes([2]) + (1).to_bytes(4, "little")))
                os.write(m, frame(contact(b"\xaa\xbb\xcc\xdd\xee\xff" + b"\x01" * 26, 1, "DL1ABC Mobil", now - 600, 49.45, 11.08)))
                os.write(m, frame(contact(b"\x33" * 32, 3, "DO1GMT Room", now - 60, 49.123, 10.456)))
                os.write(m, frame(bytes([4]) + (1001).to_bytes(4, "little")))
            elif c == 0x0A:
                os.write(m, frame(queue.pop(0) if queue else bytes([10])))
            else:
                os.write(m, frame(bytes([1, 1])))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "companion")
