#!/usr/bin/env python3
"""
Whisper STT → Gemini text API → Google TTS demo.

Tests if Whisper + text Gemini + TTS can compete with Gemini Live latency.

Usage:
    cd ~/nexus && source venv/bin/activate
    python voice/whisper_gemini_demo.py
"""

import os
import sys
import time
import queue
import tempfile
import wave
import subprocess

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

import numpy as np
import sounddevice as sd
from loguru import logger

logger.remove(0)
logger.add(sys.stderr, level="INFO")

# =============================================================================
# Config
# =============================================================================

SAMPLE_RATE = 16000
SILENCE_SECS = 0.6
SPEECH_THRESHOLD = 50
CHUNK_DURATION = 0.1  # 100ms chunks


# =============================================================================
# Continuous mic recording with VAD
# =============================================================================

def record_utterance() -> np.ndarray | None:
    """Record from mic using a continuous stream. Returns int16 numpy array."""
    chunk_samples = int(SAMPLE_RATE * CHUNK_DURATION)
    silence_chunks = int(SILENCE_SECS / CHUNK_DURATION)
    audio_queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        audio_queue.put(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=chunk_samples, callback=callback):

        # Wait for speech
        while True:
            data = audio_queue.get()
            rms = np.sqrt(np.mean(data.astype(float) ** 2))
            if rms > SPEECH_THRESHOLD:
                break

        # Collect speech until silence
        frames = [data.flatten()]
        silent_count = 0

        while True:
            data = audio_queue.get()
            frames.append(data.flatten())
            rms = np.sqrt(np.mean(data.astype(float) ** 2))

            if rms < SPEECH_THRESHOLD:
                silent_count += 1
                if silent_count >= silence_chunks:
                    break
            else:
                silent_count = 0

            if len(frames) * CHUNK_DURATION > 30:
                break

    return np.concatenate(frames)


# =============================================================================
# STT — Whisper
# =============================================================================

def load_whisper():
    from faster_whisper import WhisperModel
    print("  Loading Whisper...", end=" ", flush=True)
    t = time.time()
    model = WhisperModel("small", device="cpu", compute_type="int8")
    print(f"done ({time.time()-t:.1f}s)")
    return model


def transcribe(model, audio: np.ndarray) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio.tobytes())
    try:
        segments, _ = model.transcribe(path, language="en")
        return " ".join(s.text.strip() for s in segments).strip()
    finally:
        os.unlink(path)


# =============================================================================
# TTS — Google Cloud
# =============================================================================

def tts_speak(text: str):
    from google.cloud import texttospeech
    client = texttospeech.TextToSpeechClient()

    # Split into sentences — start playing first while rest synthesize
    sentences = []
    current = ""
    for char in text:
        current += char
        if char in ".!?" and len(current.strip()) > 5:
            sentences.append(current.strip())
            current = ""
    if current.strip():
        sentences.append(current.strip())

    for sentence in sentences:
        audio = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=sentence),
            voice=texttospeech.VoiceSelectionParams(
                language_code="en-US", name="en-US-Neural2-J",
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                speaking_rate=1.1,
            ),
        ).audio_content

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio)
            path = f.name
        subprocess.run(["afplay", path], timeout=30)
        os.unlink(path)


# =============================================================================
# LLM — Gemini text
# =============================================================================

def gemini_respond(client, history: list, user_text: str) -> str:
    from google.genai.types import Content, Part, GenerateContentConfig

    history.append(Content(role="user", parts=[Part(text=user_text)]))

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=history,
        config=GenerateContentConfig(
            system_instruction="You are Jarvis, a concise voice assistant. Keep responses under 3 sentences. Natural speech, no markdown.",
            temperature=0.7,
        ),
    )

    reply = response.text or ""
    history.append(Content(role="model", parts=[Part(text=reply)]))

    if len(history) > 8:
        history[:] = history[-6:]

    return reply


# =============================================================================
# Main
# =============================================================================

def main():
    from google import genai

    whisper = load_whisper()
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    history = []

    print("\n  Whisper → Gemini → TTS. Just talk. Ctrl+C to quit.\n")
    print("  Listening...", flush=True)

    while True:
        try:
            audio = record_utterance()
            if audio is None:
                print("  (no audio captured)")
                continue

            duration = len(audio) / SAMPLE_RATE
            print(f"  Captured {duration:.1f}s audio, transcribing...", flush=True)

            t0 = time.time()
            text = transcribe(whisper, audio)
            t_stt = time.time() - t0

            if not text or len(text.strip()) < 2:
                print(f"  (empty transcription, listening again...)")
                continue

            print(f"  You: {text}  ({t_stt:.2f}s)", flush=True)

            t1 = time.time()
            reply = gemini_respond(client, history, text)
            t_llm = time.time() - t1

            print(f"  Jarvis: {reply}  ({t_llm:.2f}s)", flush=True)

            t2 = time.time()
            tts_speak(reply)
            t_tts = time.time() - t2

            total = t_stt + t_llm + t_tts
            print(f"  [{t_stt:.2f}s STT + {t_llm:.2f}s LLM + {t_tts:.2f}s TTS = {total:.2f}s]\n")
            print("  Listening...", flush=True)

        except KeyboardInterrupt:
            break

    print("\nDone.")


if __name__ == "__main__":
    main()
