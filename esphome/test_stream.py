"""Listen to or record the Atom's raw-PCM TCP audio stream (tcp_audio_server.h).

Usage:
    python test_stream.py 192.168.1.121                          # live playback
    python test_stream.py 192.168.1.121 --record out.wav         # record until Ctrl+C
    python test_stream.py 192.168.1.121 --record out.wav --duration 10
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FFMPEG_DIR = ROOT / "ffmpeg" / "bin"

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_FMT = "s16le"  # matches the microphone's raw PCM output in atom2.yaml


def find_binary(name: str) -> str:
    local = FFMPEG_DIR / f"{name}.exe"
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    sys.exit(f"Could not find {name} (looked in {FFMPEG_DIR} and PATH)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="Atom IP address, e.g. 192.168.1.121")
    parser.add_argument("port", type=int, nargs="?", default=12345)
    parser.add_argument("--record", metavar="FILE.wav", help="Record to a WAV file instead of playing live")
    parser.add_argument("--duration", type=int, help="Seconds to record (only with --record); omit to record until Ctrl+C")
    args = parser.parse_args()

    source = f"tcp://{args.host}:{args.port}"
    input_args = ["-f", SAMPLE_FMT, "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", source]

    if args.record:
        cmd = [find_binary("ffmpeg"), *input_args]
        if args.duration:
            cmd += ["-t", str(args.duration)]
        cmd += ["-y", args.record]
        print(f"Connecting to {source} -> recording to {args.record}")
        subprocess.run(cmd, check=True)
    else:
        cmd = [find_binary("ffplay"), "-autoexit", "-nodisp", *input_args]
        print(f"Connecting to {source} - playing live audio. Ctrl+C to stop.")
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
