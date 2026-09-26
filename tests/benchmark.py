"""Optional real-engine smoke/latency check; no downloads or microphone access."""

import argparse
import json
import socket
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--wav", required=True)
    args = parser.parse_args()
    with wave.open(args.wav) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        pcm = audio.readframes(audio.getnframes())
    paths = d.Paths()
    config = d.Config(paths)
    config.values.update(model=args.model, whisper_bin=args.cli, backend="local")
    timings = {}
    transcripts = {}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        [args.server, "-m", args.model, "--host", "127.0.0.1", "--port", str(port), "-t", "4"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Whisper server failed to become ready")
                time.sleep(0.1)
        for backend in ("local", "http"):
            config.values.update(backend=backend, endpoint=f"http://127.0.0.1:{port}/inference")
            samples = []
            for _ in range(3):
                begin = time.monotonic()
                text = d.transcribe(config, pcm, paths.cache)
                samples.append(time.monotonic() - begin)
                assert "ask not" in text.lower(), text
            timings[backend] = {"seconds": samples, "median": statistics.median(samples)}
            transcripts[backend] = text
        print(
            json.dumps(
                {"audio_seconds": len(pcm) / 32000, "timings": timings, "transcripts": transcripts},
                indent=2,
            )
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


if __name__ == "__main__":
    main()
