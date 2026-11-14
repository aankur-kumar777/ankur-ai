"""Ankur AI — real-time voice conversation server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import AsyncExitStack
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from google import genai
from google.genai import types

from persona import LIVE_SYSTEM_INSTRUCTION

log = logging.getLogger("ankur-live")
logging.basicConfig(level=logging.INFO)

MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
# Charon is a male, informative prebuilt Gemini voice.
VOICE = os.getenv("LIVE_VOICE", "Charon")
ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()
MAX_SESSION_SECONDS = int(os.getenv("MAX_SESSION_SECONDS", "600"))
GREET = os.getenv("GREET", "1") != "0"

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"
if not INDEX_FILE.exists():
    # Also support the simple flat layout used by some local setups.
    INDEX_FILE = BASE_DIR / "index.html"

app = FastAPI(title="Ankur AI")
_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set on the server.")
        _client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
    return _client


def live_configs() -> list[types.LiveConnectConfig]:
    """Try the preferred realtime settings, then a simpler config with same voice."""
    base = dict(
        response_modalities=["AUDIO"],
        system_instruction=LIVE_SYSTEM_INSTRUCTION,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    speech = types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
        )
    )
    vad = types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            silence_duration_ms=600,
        )
    )
    # Keep the selected voice in every fallback; never silently fall back to Kore.
    return [
        types.LiveConnectConfig(**base, speech_config=speech, realtime_input_config=vad),
        types.LiveConnectConfig(**base, speech_config=speech),
    ]


async def open_session(stack: AsyncExitStack):
    client = get_client()
    last_error: Exception | None = None
    for config in live_configs():
        try:
            return await stack.enter_async_context(
                client.aio.live.connect(model=MODEL, config=config)
            )
        except Exception as exc:
            log.warning("Gemini Live connection attempt failed: %s", exc)
            last_error = exc
    raise RuntimeError(f"Could not open a Live session with {MODEL}: {last_error}")


@app.get("/")
async def index():
    return FileResponse(INDEX_FILE)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/config")
async def config():
    return JSONResponse({"needs_passcode": bool(ACCESS_CODE)})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    if ACCESS_CODE and not secrets.compare_digest(
        ws.query_params.get("code", ""), ACCESS_CODE
    ):
        await ws.send_json({"type": "error", "message": "That passcode wasn't accepted."})
        await ws.close(code=4401)
        return

    async with AsyncExitStack() as stack:
        try:
            session = await open_session(stack)
        except Exception:
            log.exception("Could not open Gemini Live session")
            await ws.send_json(
                {"type": "error", "message": "Couldn't start the call. Please try again."}
            )
            await ws.close()
            return

        await ws.send_json({"type": "ready"})
        if GREET:
            await session.send_realtime_input(
                text=(
                    "The interviewer has just joined the call. Greet them warmly "
                    "in one short sentence as Ankur and invite their first question."
                )
            )

        mic_logged = False
        mic_rate = [16000]

        async def browser_to_gemini():
            nonlocal mic_logged
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes"):
                    if not mic_logged:
                        mic_logged = True
                        log.info("Receiving microphone audio")
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=msg["bytes"], mime_type=f"audio/pcm;rate={mic_rate[0]}"
                        )
                    )
                elif msg.get("text"):
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "audio_format":
                        try:
                            rate = int(data.get("rate", 16000))
                            if 8000 <= rate <= 96000:
                                mic_rate[0] = rate
                        except (TypeError, ValueError):
                            pass
                    elif data.get("type") == "audio_end":
                        await session.send_realtime_input(audio_stream_end=True)

        async def gemini_to_browser():
            stats = {"audio": 0, "user": 0, "model": 0, "interrupted": 0, "turns": 0}
            while True:
                async for response in session.receive():
                    content = response.server_content
                    if not content:
                        continue
                    if content.interrupted:
                        stats["interrupted"] += 1
                        await ws.send_json({"type": "interrupted"})
                    if content.model_turn:
                        for part in content.model_turn.parts or []:
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                stats["audio"] += 1
                                await ws.send_bytes(inline.data)
                    if content.input_transcription and content.input_transcription.text:
                        stats["user"] += 1
                        await ws.send_json(
                            {"type": "user_text", "text": content.input_transcription.text}
                        )
                    if content.output_transcription and content.output_transcription.text:
                        stats["model"] += 1
                        await ws.send_json(
                            {"type": "model_text", "text": content.output_transcription.text}
                        )
                    if content.turn_complete:
                        stats["turns"] += 1
                        log.info("Conversation turn completed: %s", stats)
                        await ws.send_json({"type": "turn_complete"})

        tasks = [
            asyncio.create_task(browser_to_gemini()),
            asyncio.create_task(gemini_to_browser()),
        ]
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=MAX_SESSION_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await ws.send_json(
                    {"type": "error", "message": "This call has ended. Start a new call when you're ready."}
                )
            for task in done:
                try:
                    exc = task.exception()
                except asyncio.CancelledError:
                    exc = None
                if exc and not isinstance(exc, WebSocketDisconnect):
                    log.error("Voice session ended unexpectedly: %r", exc)
                    try:
                        await ws.send_json(
                            {"type": "error", "message": "The call was interrupted. Please reconnect."}
                        )
                    except Exception:
                        pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await ws.close()
            except Exception:
                pass
