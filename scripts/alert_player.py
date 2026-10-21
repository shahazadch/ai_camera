#!/usr/bin/env python3
"""
Play a sound on this computer when a person walks into a camera that has
"Play sound when a person appears" turned on (and it's inside the camera's
From/To window). Works with no browser open. Python standard library only.

    python3 scripts/alert_player.py
    python3 scripts/alert_player.py --api http://localhost:8000/api/v1 --key <ALERT_PLAYER_KEY>
    python3 scripts/alert_player.py --test     # just play the sound once

The key must match ALERT_PLAYER_KEY on the backend (default: alert-dev-key-change-me).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

SOUND_PATH = Path.home() / ".sentinelvision" / "person_alert.wav"
PER_CAMERA_COOLDOWN_SEC = 10.0


def build_chime(path: Path) -> None:
    """Two rising tones with soft edges, ~0.9 s. Distinct from the stairs horn."""
    rate = 44100
    notes = [(880.0, 0.28), (1318.5, 0.5)]
    frames = bytearray()
    for freq, length in notes:
        count = int(rate * length)
        for i in range(count):
            t = i / rate
            fade = min(1.0, i / (rate * 0.01), (count - i) / (rate * 0.12))
            sample = math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)
            frames += struct.pack("<h", int(0.55 * fade * sample / 1.3 * 32767))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))


def play(path: Path) -> None:
    system = platform.system()
    if system == "Windows":
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        return
    if system == "Darwin":
        player = ["afplay", str(path)]
    else:
        tool = shutil.which("paplay") or shutil.which("aplay")
        if not tool:
            print("\a", end="", flush=True)  # terminal bell fallback
            return
        player = [tool, str(path)]
    subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fetch(api: str, key: str) -> list[dict]:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/cameras/person-alerts/",
        headers={"X-Alert-Key": key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response).get("cameras", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.getenv("SENTINEL_API", "http://localhost:8000/api/v1"))
    parser.add_argument("--key", default=os.getenv("ALERT_PLAYER_KEY", "alert-dev-key-change-me"))
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between checks")
    parser.add_argument("--test", action="store_true", help="play the sound once and exit")
    args = parser.parse_args()

    if not SOUND_PATH.is_file():
        build_chime(SOUND_PATH)
    if args.test:
        play(SOUND_PATH)
        time.sleep(1.2)
        return 0

    print(f"Person alert player watching {args.api} (Ctrl+C to stop)", flush=True)
    last_seq: dict[str, int] = {}
    last_played: dict[str, float] = {}
    connected = None
    while True:
        try:
            cameras = fetch(args.api, args.key)
            if connected is not True:
                print(f"Connected. {len(cameras)} camera(s) with person alerts on.", flush=True)
                connected = True
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                print("Backend rejected the key. Set --key to the backend's ALERT_PLAYER_KEY.", flush=True)
                return 1
            cameras = None
        except (urllib.error.URLError, OSError, ValueError):
            cameras = None
        if cameras is None:
            if connected is not False:
                print("Backend not reachable, retrying…", flush=True)
                connected = False
            time.sleep(3)
            continue

        now = time.monotonic()
        for cam in cameras:
            cid = cam["camera_id"]
            seq = int(cam.get("seq") or 0)
            previous = last_seq.get(cid)
            last_seq[cid] = seq
            # First sight of a camera, or backend restarted (counter reset): baseline only.
            if previous is None or seq <= previous:
                continue
            if not cam.get("active"):
                continue
            if now - last_played.get(cid, -1e9) < PER_CAMERA_COOLDOWN_SEC:
                continue
            last_played[cid] = now
            play(SOUND_PATH)
            print(f"{time.strftime('%H:%M:%S')}  Person on {cam.get('name') or cid}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
