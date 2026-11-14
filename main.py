"""Ankur AI — real-time voice server.

Browser  <--WebSocket (PCM audio + JSON)-->  this server  <--Live API-->  Gemini

The API key stays on the server. Run locally:
    export GEMINI_API_KEY=...
    uvicorn main:app --reload --port 8080
"""

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

from persona import LIVE_SYSTEM_INSTRUCTION, QUICK_QUESTIONS

log = logging.getLogger("ankur-live")
logging.basicConfig(level=logging.INFO)
try:
    from importlib.metadata import version as _v
    log.info("google-genai %s", _v("google-genai"))
except Exception:
    pass

MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
VOICE = os.getenv("LIVE_VOICE", "Kore")
ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()          # optional: protects your API quota
MAX_SESSION_SECONDS = int(os.getenv("MAX_SESSION_SECONDS", "600"))
GREET = os.getenv("GREET", "1") != "0"

STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="Ankur AI Live")

_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set on the server.")
        # The Live endpoint is documented under v1beta. Pinning it avoids a
        # "HTTP 404" on the WebSocket handshake when the SDK defaults elsewhere.
        _client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
    return _client


def live_configs() -> list[types.LiveConnectConfig]:
    """Preferred config first; plainer fallbacks if the API rejects an optional field."""
    base = dict(
        response_modalities=["AUDIO"],
        system_instruction=LIVE_SYSTEM_INSTRUCTION,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    voice = types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
        )
    )
    # Answer quickly once the speaker stops talking.
    vad = types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            silence_duration_ms=600,
        )
    )
    return [
        types.LiveConnectConfig(**base, speech_config=voice, realtime_input_config=vad),
        types.LiveConnectConfig(**base, speech_config=voice),
        types.LiveConnectConfig(**base),
    ]


async def open_session(stack: AsyncExitStack):
    client = get_client()
    last: Exception | None = None
    for cfg in live_configs():
        try:
            return await stack.enter_async_context(
                client.aio.live.connect(model=MODEL, config=cfg)
            )
        except Exception as exc:  # try the simpler config
            log.warning("Live connect failed (%s); trying fallback config", exc)
            last = exc
    raise RuntimeError(f"Could not open a Live session with {MODEL}: {last}")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/config")
async def config():
    return JSONResponse(
        {"needs_code": bool(ACCESS_CODE), "quick_questions": QUICK_QUESTIONS}
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    if ACCESS_CODE and not secrets.compare_digest(
        ws.query_params.get("code", ""), ACCESS_CODE
    ):
        await ws.send_json({"type": "error", "message": "Wrong access code."})
        await ws.close(code=4401)
        return

    async with AsyncExitStack() as stack:
        try:
            session = await open_session(stack)
        except Exception as exc:
            log.exception("open_session failed")
            await ws.send_json({"type": "error", "message": str(exc)[:400]})
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

        mic_logged: list[int] = []
        mic_rate = [16000]  # browser tells us its real capture rate; Gemini resamples

        async def browser_to_gemini():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes"):
                    if not mic_logged:
                        mic_logged.append(1)
                        log.info("receiving microphone audio from browser")
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
                    if data.get("type") == "text" and data.get("text"):
                        await session.send_realtime_input(text=str(data["text"])[:1000])
                    elif data.get("type") == "audio_format":
                        try:
                            rate = int(data.get("rate", 16000))
                            if 8000 <= rate <= 96000:
                                mic_rate[0] = rate
                                log.info("microphone sample rate: %s Hz", rate)
                        except (TypeError, ValueError):
                            pass
                    elif data.get("type") == "audio_end":
                        await session.send_realtime_input(audio_stream_end=True)

        async def gemini_to_browser():
            stats = {"audio": 0, "user": 0, "model": 0, "interrupted": 0, "turns": 0, "other": 0}
            while True:  # receive() ends after each turn, so loop for the whole call
                async for response in session.receive():
                    sc = response.server_content
                    if not sc:
                        stats["other"] += 1
                        if stats["other"] <= 5:
                            log.info("non-content message: %s", response.model_dump(exclude_none=True).keys())
                        continue
                    if sc.interrupted:
                        stats["interrupted"] += 1
                        log.info("Gemini reported INTERRUPTED (%s so far)", stats["interrupted"])
                        await ws.send_json({"type": "interrupted"})
                    if sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                stats["audio"] += 1
                                if stats["audio"] == 1:
                                    log.info("first audio chunk from Gemini (%s bytes)", len(inline.data))
                                await ws.send_bytes(inline.data)
                    if sc.input_transcription and sc.input_transcription.text:
                        stats["user"] += 1
                        await ws.send_json(
                            {"type": "user_text", "text": sc.input_transcription.text}
                        )
                    if sc.output_transcription and sc.output_transcription.text:
                        stats["model"] += 1
                        await ws.send_json(
                            {"type": "model_text", "text": sc.output_transcription.text}
                        )
                    if sc.turn_complete:
                        stats["turns"] += 1
                        log.info("turn complete: %s", stats)
                        if stats["user"] == 0 and stats["turns"] > 1:
                            log.warning("Gemini has not produced any transcript of the user's speech yet")
                        await ws.send_json({"type": "turn_complete"})

        tasks = [
            asyncio.create_task(browser_to_gemini()),
            asyncio.create_task(gemini_to_browser()),
        ]
        try:
            done, _ = await asyncio.wait(
                tasks, timeout=MAX_SESSION_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                await ws.send_json(
                    {"type": "error", "message": "Session time limit reached. Start a new call."}
                )
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    log.warning("Session ended with error: %r", exc)
                    try:
                        await ws.send_json({"type": "error", "message": str(exc)[:300]})
                    except Exception:
                        pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await ws.close()
            except Exception:
                pass
