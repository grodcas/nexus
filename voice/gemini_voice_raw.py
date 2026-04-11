#!/usr/bin/env python3
"""
Bare Gemini Live voice chat. Mic in, voice out. Nothing else.

Usage:
    cd ~/nexus && source venv/bin/activate
    python voice/gemini_voice_raw.py
"""

import asyncio
import os

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

from google import genai
from google.genai import types
import pyaudio


async def main():
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
    )

    pa = pyaudio.PyAudio()
    mic = pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                  input=True, frames_per_buffer=1024)
    spk = pa.open(format=pyaudio.paInt16, channels=1, rate=24000,
                  output=True, frames_per_buffer=4096)

    print("\n  Gemini Live — just talk. Ctrl+C to quit.\n")

    async with client.aio.live.connect(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        config=config,
    ) as session:

        async def send():
            loop = asyncio.get_event_loop()
            while True:
                data = await loop.run_in_executor(None, mic.read, 1024, False)
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                )

        async def recv():
            while True:
                async for msg in session.receive():
                    if msg.data:
                        spk.write(msg.data)

        await asyncio.gather(send(), recv())

    mic.close()
    spk.close()
    pa.terminate()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDone.")
