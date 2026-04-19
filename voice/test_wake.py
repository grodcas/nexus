#!/usr/bin/env python3
"""
Wake-phrase STT diagnostic.

Prompts "Say WAKE UP" 10 times, records ~3s per attempt with the
same audio settings as jarvis's wake listener, transcribes via the
same Whisper pipeline, and matches against the same trigger set.
A miss here is a real-world miss too — the only difference vs.
jarvis is that this script uses one clean 3s window instead of
sliding 2s windows, so we can isolate whether misses are from
Whisper (low-quality transcription) or from window boundaries
(wake phrase straddles two 2s windows).
"""
import os
import re
import subprocess
import sys
import time

import numpy as np
import pyaudio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio import get_whisper, transcribe

SAMPLE_RATE = 16000
CHUNK = 960
RECORD_S = 3.0
ATTEMPTS = 10
WAKE_TRIGGERS = tuple(
    t.strip().lower()
    for t in os.environ.get("NEXUS_WAKE_PHRASES", "wake up").split(",")
    if t.strip()
)


def has_trigger(transcript: str) -> bool:
    if not transcript:
        return False
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in WAKE_TRIGGERS) + r")\b"
    return re.search(pattern, transcript, re.IGNORECASE) is not None


def record(pa: pyaudio.PyAudio, seconds: float) -> np.ndarray:
    mic = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    n_chunks = int(SAMPLE_RATE * seconds / CHUNK)
    frames = [mic.read(CHUNK, exception_on_overflow=False) for _ in range(n_chunks)]
    mic.close()
    return np.frombuffer(b"".join(frames), dtype=np.int16)


def main() -> None:
    print(f"Triggers: {WAKE_TRIGGERS}")
    print("Loading Whisper...")
    get_whisper()
    print("Whisper ready.\n")

    pa = pyaudio.PyAudio()
    results: list[tuple[bool, str]] = []
    try:
        for i in range(1, ATTEMPTS + 1):
            print(f"[{i:2d}/{ATTEMPTS}] Say 'WAKE UP' at the beep...")
            time.sleep(0.6)
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Tink.aiff"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.15)
            t0 = time.monotonic()
            audio = record(pa, RECORD_S)
            rec_ms = (time.monotonic() - t0) * 1000
            t0 = time.monotonic()
            try:
                text = transcribe(audio) or ""
            except Exception as e:
                text = f"<transcribe error: {e}>"
            stt_ms = (time.monotonic() - t0) * 1000
            hit = has_trigger(text)
            results.append((hit, text))
            status = "PASS" if hit else "FAIL"
            print(
                f"    heard: {text!r}  |  rec {rec_ms:.0f}ms  stt {stt_ms:.0f}ms  "
                f"->  {status}\n"
            )
    finally:
        pa.terminate()

    passed = sum(1 for hit, _ in results if hit)
    print(f"=== {passed}/{ATTEMPTS} passed ===")
    for i, (hit, text) in enumerate(results, 1):
        mark = "OK" if hit else "--"
        print(f"  [{mark}] {i:2d}  {text!r}")


if __name__ == "__main__":
    main()
