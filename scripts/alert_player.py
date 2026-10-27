#!/usr/bin/env python3
"""
Play a sound on this computer when a camera rule with a sound fires
(e.g. "Night intruder" → siren). Works with no browser open.
Python standard library only.

    python3 scripts/alert_player.py
    python3 scripts/alert_player.py --api http://localhost:8005/api/v1 --key <ALERT_PLAYER_KEY>
    python3 scripts/alert_player.py --test siren          # play a built-in tune
    python3 scripts/alert_player.py --say "Hello"         # check the voice

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
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

SOUND_DIR = Path.home() / ".sentinelvision" / "sounds"
RATE = 44100
BUILTIN_SOUNDS = ("chime", "bell", "doorbell", "beep", "horn", "alarm", "siren")
# When several rules fire in one poll, play only the most urgent tune once.
PRIORITY = ("siren", "alarm", "horn", "custom", "bell", "doorbell", "beep", "chime")
CUSTOM_PREFIX = "custom:"
EXT_FOR_TYPE = {
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a", "audio/aiff": ".aiff", "audio/x-aiff": ".aiff", "audio/ogg": ".ogg",
}


def _tone(freq_at, seconds: float, level: float = 0.5, harmonic: float = 0.3, decay: float = 0.0) -> bytearray:
    """freq_at(t) -> Hz. ``decay`` > 0 rings out like a struck bell; fades stop clicks."""
    frames = bytearray()
    count = int(RATE * seconds)
    phase = 0.0
    for i in range(count):
        t = i / RATE
        phase += 2 * math.pi * freq_at(t) / RATE
        fade = min(1.0, i / (RATE * 0.005), (count - i) / (RATE * 0.05))
        ring = math.exp(-decay * t) if decay else 1.0
        sample = (math.sin(phase) + harmonic * math.sin(2 * phase)) / (1 + harmonic)
        frames += struct.pack("<h", int(level * fade * ring * sample * 32767))
    return frames


def _silence(seconds: float) -> bytearray:
    return bytearray(int(RATE * seconds) * 2)


def _build(name: str) -> bytes:
    if name == "horn":  # long low blast
        return bytes(_tone(lambda t: 110.0 - 25.0 * t, 1.6, level=0.7, harmonic=0.8))
    if name == "siren":  # rising/falling sweeps
        sweep = lambda t: 650.0 + 450.0 * math.sin(math.pi * (t % 0.9) / 0.9)  # noqa: E731
        return bytes(_tone(sweep, 1.8, level=0.6, harmonic=0.2))
    if name == "bell":  # one struck bell ringing out
        return bytes(_tone(lambda t: 660.0, 1.8, level=0.7, harmonic=0.6, decay=2.2))
    if name == "doorbell":  # ding-dong
        return bytes(
            _tone(lambda t: 659.3, 0.7, level=0.65, harmonic=0.4, decay=3.0)
            + _tone(lambda t: 523.3, 1.1, level=0.65, harmonic=0.4, decay=2.5)
        )
    if name == "beep":  # three short beeps
        beep = _tone(lambda t: 1000.0, 0.14, level=0.5, harmonic=0.0)
        return bytes(beep + _silence(0.1) + beep + _silence(0.1) + beep)
    if name == "alarm":  # fast two-tone alarm
        return bytes(_tone(lambda t: 880.0 if int(t / 0.15) % 2 == 0 else 660.0, 2.0, level=0.55, harmonic=0.9))
    # chime: two rising notes
    return bytes(_tone(lambda t: 880.0, 0.28) + _tone(lambda t: 1318.5, 0.5))


def sound_path(name: str) -> Path:
    """Built-in tune file; replace it with your own .wav of the same name to change it."""
    name = name if name in BUILTIN_SOUNDS else "chime"
    path = SOUND_DIR / f"{name}.wav"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(_build(name))
    return path


def custom_sound_path(api: str, key: str, sound_id: str) -> Path | None:
    """Download an uploaded tune once and keep it in the sound folder."""
    cached = sorted(SOUND_DIR.glob(f"custom-{sound_id}.*"))
    if cached:
        return cached[0]
    request = urllib.request.Request(
        f"{api.rstrip('/')}/camera-rules/sounds/{sound_id}/file/", headers={"X-Alert-Key": key}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            data = response.read()
    except (urllib.error.URLError, OSError):
        return None
    SOUND_DIR.mkdir(parents=True, exist_ok=True)
    path = SOUND_DIR / f"custom-{sound_id}{EXT_FOR_TYPE.get(content_type, '.wav')}"
    path.write_bytes(data)
    return path


def _player_command(path: Path) -> list[str] | None:
    system = platform.system()
    if system == "Darwin":
        return ["afplay", str(path)]
    if system == "Windows":
        return None
    tool = shutil.which("paplay") or shutil.which("aplay")
    return [tool, str(path)] if tool else None


def _speech_command(text: str) -> list[str] | None:
    system = platform.system()
    if system == "Darwin":
        return ["say", text]
    if system == "Windows":
        quoted = text.replace("'", "''")
        return [
            "powershell", "-NoProfile", "-Command",
            "Add-Type -AssemblyName System.Speech; "
            f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{quoted}')",
        ]
    tool = shutil.which("spd-say") or shutil.which("espeak")
    return [tool, text] if tool else None


def _windows_media_command(path: Path) -> list[str]:
    uri = path.resolve().as_uri().replace("'", "''")
    script = (
        "Add-Type -AssemblyName PresentationCore; "
        "$p = New-Object System.Windows.Media.MediaPlayer; "
        f"$p.Open([uri]'{uri}'); $p.Play(); "
        # Wait until the length is known (max ~5 s), then for the tune to finish (max 30 s).
        "$i = 0; while (-not $p.NaturalDuration.HasTimeSpan -and $i -lt 50) { Start-Sleep -Milliseconds 100; $i++ }; "
        "if ($p.NaturalDuration.HasTimeSpan) { "
        "Start-Sleep -Milliseconds ([Math]::Min(30000, $p.NaturalDuration.TimeSpan.TotalMilliseconds + 200)) }; "
        "$p.Close()"
    )
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def _play_blocking(path: Path | None) -> None:
    if path is None:
        return
    if platform.system() == "Windows":
        if path.suffix.lower() == ".wav":
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME)
            return
        # mp3 / m4a / aiff: Windows' own media player via PowerShell (no installs).
        subprocess.run(
            _windows_media_command(path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    command = _player_command(path)
    if command is None:
        print("\a", end="", flush=True)  # terminal bell fallback
        return
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def announce(path: Path | None, speech: list[str]) -> None:
    """Play the tune, then speak each line — in the background so polling keeps going."""

    def run() -> None:
        _play_blocking(path)
        for text in speech:
            command = _speech_command(text)
            if command:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

    threading.Thread(target=run, daemon=True).start()


def urgency(sound: str) -> int:
    kind = "custom" if sound.startswith(CUSTOM_PREFIX) else sound
    return PRIORITY.index(kind) if kind in PRIORITY else len(PRIORITY)


def resolve_sound(api: str, key: str, sound: str) -> Path | None:
    if not sound:
        return None
    if sound.startswith(CUSTOM_PREFIX):
        return custom_sound_path(api, key, sound[len(CUSTOM_PREFIX):]) or sound_path("chime")
    return sound_path(sound)


def fetch(api: str, key: str, after: int | None) -> dict:
    query = f"?after={after}" if after is not None else ""
    request = urllib.request.Request(
        f"{api.rstrip('/')}/camera-rules/hits/{query}",
        headers={"X-Alert-Key": key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.getenv("SENTINEL_API", "http://localhost:8005/api/v1"))
    parser.add_argument("--key", default=os.getenv("ALERT_PLAYER_KEY", "alert-dev-key-change-me"))
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between checks")
    parser.add_argument(
        "--test", nargs="?", const="chime", choices=BUILTIN_SOUNDS,
        help="play one built-in tune and exit",
    )
    parser.add_argument("--say", metavar="TEXT", help="speak TEXT once and exit (checks the voice)")
    parser.add_argument("--log", metavar="FILE", help="append output to FILE (for windowless auto-start)")
    args = parser.parse_args()

    if args.log:
        log_path = Path(args.log).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 - lives for the process
        sys.stdout = sys.stderr = log_file

    if args.test or args.say:
        _play_blocking(sound_path(args.test) if args.test else None)
        if args.say:
            command = _speech_command(args.say)
            if command:
                subprocess.run(command, check=False)
            else:
                print("No speech tool found on this computer.")
        return 0

    print(f"Alert player watching {args.api} (Ctrl+C to stop)", flush=True)
    after: int | None = None
    connected = None
    while True:
        try:
            data = fetch(args.api, args.key, after)
            if connected is not True:
                print("Connected. Waiting for camera rules to fire…", flush=True)
                connected = True
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                print("Backend rejected the key. Set --key to the backend's ALERT_PLAYER_KEY.", flush=True)
                return 1
            data = None
        except (urllib.error.URLError, OSError, ValueError):
            data = None
        if data is None:
            if connected is not False:
                print("Backend not reachable, retrying…", flush=True)
                connected = False
            time.sleep(3)
            continue

        latest = int(data.get("latest") or 0)
        # First poll, or the backend restarted and its ids started over: start from now.
        if after is None or latest < after:
            after = latest
            time.sleep(args.interval)
            continue
        sounds: list[str] = []
        speech: list[str] = []
        for hit in data.get("hits", []):
            after = max(after, int(hit["id"]))
            print(f"{time.strftime('%H:%M:%S')}  {hit.get('message')}", flush=True)
            if hit.get("sound"):
                sounds.append(hit["sound"])
            if hit.get("speech") and hit["speech"] not in speech:
                speech.append(hit["speech"])
        if sounds or speech:
            # Several rules at once: one tune (most urgent), then up to 3 spoken lines.
            sound = min(sounds, key=urgency) if sounds else ""
            announce(resolve_sound(args.api, args.key, sound), speech[:3])
        time.sleep(args.interval)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
