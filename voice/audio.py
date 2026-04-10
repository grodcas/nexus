#!/usr/bin/env python3
"""
Audio primitives for Jarvis — STT, TTS, recording, wake word, keywords.

Reusable across all voice modes (Jarvis, Claude, Claudia).
"""

import os
import random
import subprocess
import tempfile
import time
import wave

import numpy as np
import sounddevice as sd
from loguru import logger

# =============================================================================
# Constants
# =============================================================================

SAMPLE_RATE = 16000
SILENCE_DURATION = 1.5       # seconds of silence to end recording
MAX_RECORD_DURATION = 60     # max single recording
WAKEWORD_THRESHOLD = 0.7
INTERRUPT_RMS_THRESHOLD = 600  # for speak_interruptible (not used in Claude mode)
SPEECH_RMS_THRESHOLD = 150     # normal speech detection
ACK_CACHE_DIR = os.path.expanduser("~/.nexus/audio_cache")


# =============================================================================
# Keyword detection
# =============================================================================

# Order matters — check longer/more specific patterns first
_KEYWORD_ORDER = [
    "over_claudia", "over_claude",
    "stop_claudia", "stop_claude",
    "hey_claudia", "hey_claude",
    "close_session", "jarvis",
]

_KEYWORD_PATTERNS = {
    "hey_claude":     ["hey claude", "hey cloud", "hey claud"],
    "hey_claudia":    ["hey claudia", "hey cloudia"],
    "over_claude":    ["over claude", "over cloud", "overcloud", "overclone",
                       "over claud", "over clone", "over, claud",
                       "over club", "overglot", "over clod", "over glob",
                       "claude over", "cloud over", "clod over", "cloth over",
                       "claud over", "clone over", "club over", "glob over"],
    "over_claudia":   ["over claudia", "over cloudia", "overcloudia",
                       "claudia over", "cloudia over"],
    "stop_claude":    ["claude stop", "stop claude", "cloud stop", "stop cloud",
                       "claud stop", "stop claud"],
    "stop_claudia":   ["claudia stop", "stop claudia", "cloudia stop", "stop cloudia"],
    "jarvis":         ["jarvis", "yervis", "yarvis", "charvis"],
    "close_session":  ["close session", "close the session"],
}


def detect_keyword(text: str) -> str | None:
    """Detect a keyword in transcribed text. Returns keyword key or None."""
    norm = text.lower()
    for ch in ",.!?;:":
        norm = norm.replace(ch, "")
    norm = " ".join(norm.split())  # collapse whitespace

    for key in _KEYWORD_ORDER:
        for pattern in _KEYWORD_PATTERNS[key]:
            if pattern in norm:
                return key
    return None


def has_keyword(text: str, keyword_key: str) -> bool:
    """Check if a specific keyword is present in text."""
    norm = text.lower()
    for ch in ",.!?;:":
        norm = norm.replace(ch, "")
    norm = " ".join(norm.split())
    for pattern in _KEYWORD_PATTERNS[keyword_key]:
        if pattern in norm:
            return True
    return False


def strip_keyword(text: str, keyword_key: str) -> str:
    """Remove keyword pattern from text, return remaining text."""
    import re
    lower = text.lower()

    for pattern in _KEYWORD_PATTERNS[keyword_key]:
        # Build regex that allows optional punctuation/spaces between words
        words = pattern.split()
        regex = r"[,.\s]*".join(re.escape(w) for w in words)
        match = re.search(regex, lower)
        if match:
            before = text[:match.start()].strip()
            after = text[match.end():].strip()
            result = f"{before} {after}".strip()
            return result.strip(" ,.")
    return text


# =============================================================================
# STT — faster-whisper
# =============================================================================

_whisper_model = None


def get_whisper():
    """Lazy-load Whisper model."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading Whisper model...")
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        logger.info("Whisper ready")
    return _whisper_model


def transcribe(audio: np.ndarray) -> str:
    """Transcribe int16 audio array to text."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio.tobytes())

    try:
        model = get_whisper()
        segments, _ = model.transcribe(path, language="en")
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
    finally:
        os.unlink(path)


# =============================================================================
# TTS — Google Cloud Neural2-J
# =============================================================================

_tts_client = None


def get_tts():
    """Lazy-load Google TTS client."""
    global _tts_client
    if _tts_client is None:
        from google.cloud import texttospeech
        _tts_client = texttospeech.TextToSpeechClient()
        logger.info("Google TTS ready")
    return _tts_client


def _synthesize(text: str) -> bytes:
    """Synthesize text to WAV bytes via Google Cloud TTS."""
    from google.cloud import texttospeech

    return get_tts().synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code="en-US", name="en-US-Neural2-J",
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            speaking_rate=1.05,
        ),
    ).audio_content


def _split_sentences(text: str, max_len: int = 400) -> list[str]:
    """Split text into sentence-sized chunks for streaming TTS."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    for part in text.replace(". ", ".|").replace("? ", "?|").replace("! ", "!|").split("|"):
        if len(current) + len(part) > max_len and current:
            chunks.append(current.strip())
            current = part
        else:
            current += " " + part if current else part
    if current.strip():
        chunks.append(current.strip())
    return chunks


_current_playback = None  # Current afplay subprocess — killed by hotkey


def _play_wav_bytes(audio_bytes: bytes):
    """Play WAV audio through speakers. Can be killed via Cmd+Shift+J."""
    global _current_playback
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        path = f.name
    try:
        _current_playback = subprocess.Popen(["afplay", path])
        _current_playback.wait()
        _current_playback = None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def stop_speaking():
    """Kill current TTS playback. Called by hotkey handler."""
    global _current_playback
    if _current_playback and _current_playback.poll() is None:
        _current_playback.kill()
        _current_playback = None
        logger.info("TTS stopped by hotkey (Cmd+Shift+J)")


_tts_interrupted = False


def speak(text: str):
    """Speak text via TTS. Blocks until done. Cmd+Shift+J stops it."""
    global _tts_interrupted
    _tts_interrupted = False

    if not text or not text.strip():
        return
    for chunk in _split_sentences(text):
        if _tts_interrupted:
            break
        if not chunk.strip():
            continue
        try:
            audio = _synthesize(chunk)
            _play_wav_bytes(audio)
            # Check if we were killed mid-chunk
            if _current_playback is None and not _tts_interrupted:
                _tts_interrupted = True
                break
        except Exception as e:
            logger.error(f"TTS error: {e}")
            subprocess.run(["say", chunk], timeout=30)


def speak_interruptible(text: str) -> bool:
    """
    Speak text via TTS, monitoring mic for interruption.
    Returns True if speech was interrupted by user talking.
    """
    if not text or not text.strip():
        return False

    for chunk in _split_sentences(text):
        if not chunk.strip():
            continue
        try:
            audio = _synthesize(chunk)
            if _play_with_interrupt_check(audio):
                return True
        except Exception as e:
            logger.error(f"TTS error: {e}")
    return False


def _play_with_interrupt_check(audio_bytes: bytes) -> bool:
    """Play WAV while monitoring mic for interruption. Returns True if interrupted."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        path = f.name

    try:
        proc = subprocess.Popen(["afplay", path])
        check_samples = int(SAMPLE_RATE * 0.3)  # 300ms chunks

        while proc.poll() is None:
            try:
                data = sd.rec(check_samples, samplerate=SAMPLE_RATE,
                              channels=1, dtype="int16")
                sd.wait()
                rms = np.sqrt(np.mean(data.astype(float) ** 2))
                if rms > INTERRUPT_RMS_THRESHOLD:
                    proc.kill()
                    proc.wait()
                    logger.info(f"TTS interrupted (RMS={rms:.0f})")
                    return True
            except Exception:
                break

        proc.wait()
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# =============================================================================
# Acknowledgments — pre-cached TTS phrases
# =============================================================================

GREETINGS = [
    "Yes?",
    "Tell me.",
    "Go ahead.",
    "Listening.",
    "What do you need?",
    "I'm here.",
    "Ready.",
    "Yes sir.",
]

ACKNOWLEDGMENTS = [
    "Mm, let me work on that.",
    "Noted.",
    "On it.",
    "Got it, cooking.",
    "Working on it.",
    "Alright, on it.",
    "Sure thing.",
    "Let me look into that.",
    "Understood, working.",
    "Right away.",
]


def init_ack_cache():
    """Pre-synthesize greeting and acknowledgment phrases to disk cache."""
    os.makedirs(ACK_CACHE_DIR, exist_ok=True)
    cached = 0
    for prefix, phrases in [("greet", GREETINGS), ("ack", ACKNOWLEDGMENTS)]:
        for i, text in enumerate(phrases):
            path = os.path.join(ACK_CACHE_DIR, f"{prefix}_{i}.wav")
            if not os.path.exists(path):
                try:
                    audio = _synthesize(text)
                    with open(path, "wb") as f:
                        f.write(audio)
                    cached += 1
                except Exception as e:
                    logger.error(f"Failed to cache {prefix}_{i}: {e}")
    if cached:
        logger.info(f"Cached {cached} phrases")


def play_greeting():
    """Play a random greeting phrase (blocking — so user hears it before speaking)."""
    idx = random.randint(0, len(GREETINGS) - 1)
    path = os.path.join(ACK_CACHE_DIR, f"greet_{idx}.wav")
    if os.path.exists(path):
        subprocess.run(["afplay", path], timeout=5)
    else:
        subprocess.run(["say", "Yes?"], timeout=5)


def play_ack():
    """Play a random acknowledgment phrase (blocking)."""
    idx = random.randint(0, len(ACKNOWLEDGMENTS) - 1)
    path = os.path.join(ACK_CACHE_DIR, f"ack_{idx}.wav")
    if os.path.exists(path):
        subprocess.run(["afplay", path], timeout=5)
    else:
        # Fallback
        subprocess.Popen(["say", "Got it."])


# =============================================================================
# Recording — energy-based VAD
# =============================================================================

def record_speech(silence_duration: float = SILENCE_DURATION,
                  max_duration: float = MAX_RECORD_DURATION,
                  wait_timeout: float | None = None) -> np.ndarray | None:
    """
    Record from mic until silence after speech.

    Args:
        silence_duration: seconds of silence to stop after speech starts.
        max_duration: absolute max recording time.
        wait_timeout: max seconds to wait for speech to begin. None = max_duration.

    Returns:
        int16 numpy array, or None if no speech detected.
    """
    chunk_duration = 0.1  # 100ms
    chunk_samples = int(SAMPLE_RATE * chunk_duration)
    silence_chunks = int(silence_duration / chunk_duration)
    max_chunks = int(max_duration / chunk_duration)
    wait_chunks = int((wait_timeout or max_duration) / chunk_duration)

    audio_chunks = []
    silent_count = 0
    speech_started = False
    wait_count = 0

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=chunk_samples)
    stream.start()

    try:
        for _ in range(max_chunks):
            data, _ = stream.read(chunk_samples)
            audio_chunks.append(data.copy())
            rms = np.sqrt(np.mean(data.astype(float) ** 2))

            if rms > SPEECH_RMS_THRESHOLD:
                speech_started = True
                silent_count = 0
                wait_count = 0
            elif speech_started:
                silent_count += 1
                if silent_count >= silence_chunks:
                    break
            else:
                wait_count += 1
                if wait_count >= wait_chunks:
                    break
    finally:
        stream.stop()
        stream.close()

    if not speech_started:
        return None

    audio = np.concatenate(audio_chunks).flatten()
    # Trim trailing silence
    trim = int(silence_duration * SAMPLE_RATE)
    if len(audio) > trim:
        audio = audio[:-trim]
    return audio


# =============================================================================
# Wake word — Whisper-based "hey jarvis" detection
# =============================================================================

WAKE_PHRASES = ["hey"]


def wait_for_wakeword(timeout: float = 0) -> bool:
    """
    Block until "hey jarvis" detected via Whisper.

    Listens for short speech bursts, transcribes only when energy detected,
    checks for wake phrase. Much more accurate than keyword-spotting models.

    Args:
        timeout: max seconds to wait. 0 = wait forever.

    Returns:
        True if wake word detected, False on timeout.
    """
    start = time.time()

    while True:
        if timeout > 0 and (time.time() - start) > timeout:
            return False

        audio = record_speech(silence_duration=0.8, max_duration=3.0, wait_timeout=2.0)
        if audio is None:
            logger.debug("Wake: no speech detected (silence)")
            continue

        duration = len(audio) / SAMPLE_RATE
        if duration > 4.0:
            logger.debug(f"Wake: skipping {duration:.1f}s (too long)")
            continue

        text = transcribe(audio)
        if not text:
            logger.debug("Wake: empty transcription")
            continue

        lower = text.lower().strip()
        logger.info(f"Wake heard: '{text.strip()}' ({duration:.1f}s)")
        for phrase in WAKE_PHRASES:
            if phrase in lower:
                logger.info(f"Wake word MATCHED: '{text.strip()}'")
                return True


# =============================================================================
# Global hotkey — Cmd+Shift+J stops TTS playback
# =============================================================================

def start_hotkey_listener():
    """Start background listener for Cmd+Shift+J to stop TTS."""
    try:
        from pynput import keyboard

        def on_hotkey():
            global _tts_interrupted
            _tts_interrupted = True
            stop_speaking()

        hotkey = keyboard.HotKey(
            keyboard.HotKey.parse("<cmd>+<shift>+j"),
            on_hotkey,
        )

        def on_press(key):
            hotkey.press(listener.canonical(key))

        def on_release(key):
            hotkey.release(listener.canonical(key))

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()
        logger.info("Hotkey listener started: Cmd+Shift+J to stop TTS")
    except Exception as e:
        logger.warning(f"Hotkey listener failed (non-fatal): {e}")
