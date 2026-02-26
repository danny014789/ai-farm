#!/usr/bin/env python3
"""
farmctl.py - Unified CLI for AI-farming serial + camera control.

Usage examples:
  python3 farmctl.py status --json
  python3 farmctl.py cmd help
  python3 farmctl.py light on
  python3 farmctl.py pump on --sec 8
  python3 farmctl.py camera-snap --out ~/plant.jpg
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

try:
    import serial  # type: ignore
except Exception:
    serial = None


DEFAULT_SERIAL = "/dev/ttyACM0"
DEFAULT_BAUD = 115200


def run(cmd: str, timeout: int = 15) -> tuple[int, str, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


@dataclass
class SerialClient:
    port: str = DEFAULT_SERIAL
    baud: int = DEFAULT_BAUD
    timeout_s: float = 2.0

    def send(self, command: str, read_s: float = 1.2) -> str:
        if serial is None:
            raise RuntimeError("pyserial not installed. Run: python3 -m pip install pyserial")
        with serial.Serial(self.port, self.baud, timeout=self.timeout_s) as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write((command.strip() + "\n").encode("utf-8"))
            ser.flush()
            time.sleep(read_s)
            data = ser.read_all().decode("utf-8", errors="ignore")
            return data.strip()


def parse_csv_status(line: str) -> Dict[str, Any]:
    # CSV format from Arduino printCSV():
    # co2,tempC,rh,lightRaw,soilRaw,waterOK,lightOn,heaterOn,heaterLockout,waterOn,circOn,waterRem,circRem
    # Example: 609,23.57,67.95,54,1023,1,0,0,0,0,0,0,0
    parts = [p.strip() for p in line.split(",") if p.strip() != ""]
    out: Dict[str, Any] = {"raw": line, "fields": parts}
    if len(parts) >= 5:
        try:
            out.update(
                {
                    "co2_ppm": float(parts[0]),
                    "temp_c": float(parts[1]),
                    "humidity_pct": float(parts[2]),
                    "light_raw": float(parts[3]),
                    "soil_raw": float(parts[4]),
                }
            )
        except ValueError:
            pass
    # Extended fields: relay states, water tank, heater lockout, timers
    if len(parts) >= 13:
        try:
            out.update(
                {
                    "water_tank_ok": int(parts[5]) == 1,
                    "light_on": int(parts[6]) == 1,
                    "heater_on": int(parts[7]) == 1,
                    "heater_lockout": int(parts[8]) == 1,
                    "water_pump_on": int(parts[9]) == 1,
                    "circulation_on": int(parts[10]) == 1,
                    "water_pump_remaining_sec": int(parts[11]),
                    "circulation_remaining_sec": int(parts[12]),
                }
            )
        except ValueError:
            pass
    return out


def serial_status(sc: SerialClient) -> Dict[str, Any]:
    # prefer CSV read for machine parsing
    raw = sc.send("r")
    # pick last csv-looking line
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    csv_line = ""
    for ln in reversed(lines):
        if re.match(r"^\d+([.,]\d+)?(,\s*[-+]?\d+([.,]\d+)?)+$", ln):
            csv_line = ln.replace(" ", "")
            break
    parsed = parse_csv_status(csv_line) if csv_line else {"raw": raw}
    parsed["source"] = "serial:r"
    return parsed


def camera_snap(out_path: str, timeout_ms: int = 1200) -> Dict[str, Any]:
    out_path = os.path.expanduser(out_path)
    cmd = f"rpicam-still -o {shlex.quote(out_path)} -t {int(timeout_ms)} --nopreview"
    rc, out, err = run(cmd, timeout=20)
    exists = os.path.exists(out_path)
    size = os.path.getsize(out_path) if exists else 0
    return {
        "ok": rc == 0 and exists,
        "cmd": cmd,
        "rc": rc,
        "stdout": out,
        "stderr": err,
        "path": out_path,
        "bytes": size,
    }


def act(sc: SerialClient, cmd: str) -> Dict[str, Any]:
    raw = sc.send(cmd)
    return {"ok": True, "cmd": cmd, "raw": raw}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AI-farming unified CLI")
    p.add_argument("--port", default=DEFAULT_SERIAL)
    p.add_argument("--baud", default=DEFAULT_BAUD, type=int)

    sub = p.add_subparsers(dest="sub", required=True)

    s_status = sub.add_parser("status")
    s_status.add_argument("--json", action="store_true")

    s_cmd = sub.add_parser("cmd")
    s_cmd.add_argument("text", help="raw serial command (e.g. help, p, r)")

    s_light = sub.add_parser("light")
    s_light.add_argument("state", choices=["on", "off"])

    s_heater = sub.add_parser("heater")
    s_heater.add_argument("state", choices=["on", "off"])

    s_pump = sub.add_parser("pump")
    s_pump.add_argument("state", choices=["on", "off"])
    s_pump.add_argument("--sec", type=int, default=10)

    s_circ = sub.add_parser("circulation")
    s_circ.add_argument("state", choices=["on", "off"])
    s_circ.add_argument("--sec", type=int, default=10)

    s_cam = sub.add_parser("camera-snap")
    s_cam.add_argument("--out", default="~/Pictures/plant_latest.jpg")
    s_cam.add_argument("--timeout-ms", type=int, default=1200)
    s_cam.add_argument("--json", action="store_true")

    return p


def main() -> int:
    args = build_parser().parse_args()
    sc = SerialClient(port=args.port, baud=args.baud)

    try:
        if args.sub == "status":
            data = serial_status(sc)
            if args.json:
                print(json.dumps(data, ensure_ascii=False))
            else:
                print(data)
            return 0

        if args.sub == "cmd":
            print(act(sc, args.text)["raw"])
            return 0

        if args.sub == "light":
            cmd = "lon" if args.state == "on" else "loff"
            print(act(sc, cmd)["raw"])
            return 0

        if args.sub == "heater":
            cmd = "hon" if args.state == "on" else "hoff"
            print(act(sc, cmd)["raw"])
            return 0

        if args.sub == "pump":
            cmd = f"w_on,{args.sec}" if args.state == "on" else "w_off"
            print(act(sc, cmd)["raw"])
            return 0

        if args.sub == "circulation":
            cmd = f"c_on,{args.sec}" if args.state == "on" else "c_off"
            print(act(sc, cmd)["raw"])
            return 0

        if args.sub == "camera-snap":
            data = camera_snap(args.out, args.timeout_ms)
            if args.json:
                print(json.dumps(data, ensure_ascii=False))
            else:
                print(data)
            return 0 if data.get("ok") else 2

        print("unknown subcommand", file=sys.stderr)
        return 2

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
