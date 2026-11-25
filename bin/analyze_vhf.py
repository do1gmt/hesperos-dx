#!/usr/bin/env python3
"""
Nachbearbeitung fuer VHF/UHF-Treffer (RTL-SDR):
- AM/NFM/WFM-Audio als <stem>_FM.wav bzw. _AM.wav (zum Anhoeren im Dashboard)
- APRS (AFSK1200) und POCSAG per multimon-ng, falls installiert -> _decode.json
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webapp as W  # demodulate(), scanner_config()

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "scanner.db"
OUT = BASE / "reports"
MULTIMON = shutil.which("multimon-ng")

# APRS (AFSK1200) wird in jedem FM-Treffer gesucht, nicht nur auf der APRS-Frequenz: ISS-Digipeater 145.825,
# Satelliten mit Doppler, abweichende Frequenzen anderer Länder (1.8.53). multimon-ng auf ein paar Sekunden Ton ist billig.
FM_DATA = {"fm", "relais", "dv", "aprs", "sat", "data"}
AUDIO_MODE = {"fm": "nfm", "relais": "nfm", "dv": "nfm", "aprs": "nfm", "pocsag": "nfm",
              "sat": "nfm", "data": "nfm", "am": "am", "wfm": "wfm"}


def load_iq(path):
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    sm = np.frombuffer(frames, dtype="<i2")
    sm = sm[: len(sm) // 2 * 2].reshape(-1, 2).astype(np.float64) / 32768.0
    i, q = sm[:, 0] - sm[:, 0].mean(), sm[:, 1] - sm[:, 1].mean()
    return i + 1j * q, rate


def write_wav(path, audio, rate):
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 0:
        audio = audio / peak * 0.85
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(rate))
        w.writeframes((audio * 32767).astype("<i2").tobytes())


def run_multimon(audio, rate, decoder):
    """multimon-ng braucht 22050 Hz, 16 Bit mono."""
    from scipy.signal import resample_poly
    from math import gcd
    g = gcd(22050, int(rate))
    a = resample_poly(audio, 22050 // g, int(rate) // g)
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tmp:
        tmp.write((a / (np.max(np.abs(a)) or 1) * 0.9 * 32767).astype("<i2").tobytes())
        raw = tmp.name
    try:
        r = subprocess.run([MULTIMON, "-t", "raw", "-a", decoder, "-q", raw],
                           capture_output=True, text=True, timeout=60)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip() and not l.startswith("multimon")]
    finally:
        Path(raw).unlink(missing_ok=True)
    return lines


def _checked(jpath):
    """Wurde dieser Treffer schon auf APRS geprüft? (sonst einmal nachholen – nur Treffer der letzten 3 Tage)"""
    try:
        return "AFSK1200" in json.loads(jpath.read_text(encoding="utf-8")).get("checked", [])
    except Exception:
        return False


def main():
    db = sqlite3.connect(DB)
    rows = db.execute("""
        SELECT p.id, p.rf_hz, p.recording_path, COALESCE(p.rec_center_hz, s.center_hz), s.band, COALESCE(p.mode,''),
               s.time >= datetime('now', '-3 days')
        FROM peak p JOIN scan s ON s.id = p.scan_id
        WHERE p.recording_path IS NOT NULL
          AND COALESCE(p.rec_center_hz, s.center_hz) >= 30000000
        ORDER BY p.id""").fetchall()
    db.close()
    done = 0
    for peak_id, rf_hz, recpath, center_hz, band, mode, recent in rows:
        src = Path(recpath)
        if not src.exists():
            continue
        amode = AUDIO_MODE.get(mode)
        if not amode:
            continue
        suffix = "_AM.wav" if amode == "am" else "_FM.wav"
        out = OUT / f"{src.stem}{suffix}"
        jpath = OUT / f"{src.stem}_decode.json"
        afsk = bool(MULTIMON) and mode in FM_DATA
        if out.exists() and (mode not in ("aprs", "pocsag") or jpath.exists()) and not (afsk and recent and not _checked(jpath)):
            continue
        try:
            # stückweise laden und mischen (complex64, ohne Herunterrechnen) – Aufnahmen verfolgter QSOs sind bis
            # 120 s lang; früher lagen mehrere Kopien in doppelter Genauigkeit im Speicher
            import iqload as IQ
            bb, rate = IQ.baseband(src, float(rf_hz) - float(center_hz), 10 ** 9)
            audio, arate = W.demodulate(bb, rate, amode)
            write_wav(out, audio, arate)
            print(f"{band:9s} {rf_hz/1e6:10.4f} MHz  {mode:7s} -> {out.name}")
            if mode in ("aprs", "pocsag") or afsk:
                data = {"peak_id": peak_id, "band": band, "frequency_hz": rf_hz, "decoders": []}
                if jpath.exists():
                    try:
                        data = json.loads(jpath.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                if MULTIMON:
                    pocsag = mode == "pocsag"
                    lines = run_multimon(audio, arate, "POCSAG1200" if pocsag else "AFSK1200")
                    if pocsag:
                        lines += run_multimon(audio, arate, "POCSAG512")
                    data["decoders"] = [d for d in data.get("decoders", []) if d.get("type") not in ("APRS", "POCSAG")]
                    data["checked"] = sorted(set(data.get("checked", [])) | {"POCSAG" if pocsag else "AFSK1200"})
                    if lines:
                        data["decoders"].insert(0, {
                            "type": "POCSAG" if pocsag else "APRS",
                            "confidence": 95, "text": "\n".join(lines[:40]),
                            "detail": f"multimon-ng, {len(lines)} Pakete" + ("" if mode in ("aprs", "pocsag") else " (außerhalb der APRS-Frequenz)"),
                            "messages": []})
                        print(f"          {len(lines)} Pakete dekodiert")
                else:
                    data.setdefault("notes", []).append("multimon-ng nicht installiert (sudo apt install multimon-ng)")
                jpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            done += 1
        except Exception as e:
            print(f"FEHLER {band} {rf_hz/1e6:.4f} MHz: {e}")
    print(f"VHF/UHF-Audio erzeugt: {done}")


if __name__ == "__main__":
    main()
