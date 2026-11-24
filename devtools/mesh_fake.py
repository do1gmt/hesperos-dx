#!/usr/bin/env python3
"""Nachgebildeter MeshCore-Companion-Knoten (USB) für Selbsttest und UI-Probe – ohne Hardware.

    python3 devtools/mesh_fake.py <ordner>      legt <ordner>/by-id/usb-Espressif_fake-if00 (→ pty) und
                                                <ordner>/sys/<pts>/device/idVendor = 303a an und antwortet wie
                                                ein Knoten (Kanäle, Kontakte, Nachrichten, Paket-Log).
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


def main(d):
    os.makedirs(d + "/by-id", exist_ok=True)
    m, s = pty.openpty()
    tty.setraw(m)
    name = os.ttyname(s)
    link = d + "/by-id/usb-Espressif_Systems_heltec_fake-if00"
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(name, link)
    sd = f"{d}/sys/{os.path.basename(name)}/device"
    os.makedirs(sd, exist_ok=True)
    open(sd + "/idVendor", "w").write("303a\n")
    print(link, flush=True)
    now = int(time.time())
    queue = [  # Nachrichten, die „warten“
        bytes([17, 40, 0, 0, 0, 2, 0]) + now.to_bytes(4, "little") + "DL1ABC Mobil: Hallo Mesh aus Franken".encode(),
        bytes([16, 0xF4, 0, 0]) + bytes.fromhex("aabbccddeeff") + bytes([1, 0]) + (now - 5).to_bytes(4, "little") + "Direkt an dich".encode(),
    ]
    buf, pushed = b"", False
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
                os.write(m, frame(bytes([5, 1, 22, 22]) + b"\x11" * 32 + int(49.8e6).to_bytes(4, "little", signed=True)
                                  + int(9.9e6).to_bytes(4, "little", signed=True) + bytes([0, 0, 0, 0])
                                  + (869525).to_bytes(4, "little") + (250000).to_bytes(4, "little") + bytes([11, 5])
                                  + "DO1GMT Pi".encode()))
            elif c == 0x16:
                os.write(m, frame(bytes([13, 9, 50, 8]) + bytes(4) + b"23 Oct 2026".ljust(12, b"\0")
                                  + b"Heltec V4".ljust(40, b"\0") + b"v1.9.1".ljust(20, b"\0") + bytes([0])))
            elif c == 0x1F:
                i = p[1]
                if i < 2:
                    os.write(m, frame(bytes([18, i]) + ("Public" if i == 0 else "#franken").encode().ljust(32, b"\0") + bytes(16)))
                else:
                    os.write(m, frame(bytes([1, 2])))
            elif c == 4:
                os.write(m, frame(bytes([2]) + (1).to_bytes(4, "little")))
                os.write(m, frame(contact(b"\xaa\xbb\xcc\xdd\xee\xff" + b"\x01" * 26, 1, "DL1ABC Mobil", now - 600, 49.45, 11.08)))
                os.write(m, frame(bytes([4]) + (1001).to_bytes(4, "little")))
            elif c == 0x0A:
                os.write(m, frame(queue.pop(0) if queue else bytes([10])))
            else:
                os.write(m, frame(bytes([1, 1])))


if __name__ == "__main__":
    main(sys.argv[1])
