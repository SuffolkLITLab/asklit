"""Optional paid speech; browser speech is always available without these endpoints."""
import hashlib
import hmac
import io
import json
import threading
import time
import wave
from datetime import datetime, timezone

import httpx
from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from .models import Bot, Conversation, Organization, RateBucket, Turn
from .schemas import SpeechRequest
from .security import accessible_bot, login_session, token

MAX_AUDIO_BYTES = 1024 * 1024
MAX_SECONDS = 30
MAX_SPEECH_CHARS = 4000
SLOTS = threading.BoundedSemaphore(4)


def speech_configured(settings):
    return bool(settings.speech_api_key and settings.speech_stt_model and settings.speech_tts_model)


def voice_policy(org, settings):
    return {"browser": True, "paid": bool(org.paid_voice_enabled and speech_configured(settings)),
            "max_recording_seconds": MAX_SECONDS, "max_speech_characters": MAX_SPEECH_CHARS}


def check_paid(org, settings):
    if not org.paid_voice_enabled:
        raise HTTPException(403, "Paid voice is disabled for this organization. Use browser voice.")
    if not speech_configured(settings):
        raise HTTPException(503, "Paid speech is not configured. Use browser voice.")


def normalized_wav(data):
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Record up to 30 seconds of audio (1 MB maximum).")
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames = reader.getnframes()
            if (reader.getnchannels() != 1 or reader.getframerate() != 16000
                or reader.getsampwidth() != 2 or reader.getcomptype() != "NONE"
                or not 0 < frames <= MAX_SECONDS * 16000):
                raise ValueError()
            samples = reader.readframes(frames)
            if len(samples) != frames * 2:
                raise ValueError()
        out = io.BytesIO()
        with wave.open(out, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(samples)
        return out.getvalue()
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(422, "Use mono 16 kHz PCM WAV audio, no longer than 30 seconds.") from None


def reserve_speech(db, org, subject, request, settings):
    day = datetime.now(timezone.utc).date().isoformat()
    ip = request.client.host if request.client else "unknown"
    ip_hash = hmac.new(settings.secret_key.encode(), ip.encode(), hashlib.sha256).hexdigest()[:32]
    limits = [("global", settings.speech_daily_calls), ("org:" + org.id, org.paid_voice_daily_calls),
              ("subject:" + subject, 24 if subject.startswith("chat:") else 60), ("ip:" + ip_hash, 30)]
    for label, limit in limits:
        key = f"speech:{label}:{day}"
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(RateBucket).values(key=key, count=0, expires=time.time() + 172800).on_conflict_do_nothing())
        row = db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
        if row.count >= limit:
            raise HTTPException(429, "The paid-speech allowance has been reached. Use browser voice.")
        row.count += 1


def provider_request(settings, kind, value):
    """No automatic retries: each request may incur a charge."""
    key = settings.speech_api_key
    headers = {settings.speech_key_header: "Bearer " + key if settings.speech_key_header == "Authorization" else key}
    if kind == "transcribe":
        path = "/audio/transcriptions"
        kwargs = {"data": {"model": settings.speech_stt_model}, "files": {"file": ("recording.wav", value, "audio/wav")}}
        limit = 65536
    else:
        path = "/audio/speech"
        kwargs = {"json": {"model": settings.speech_tts_model, "voice": settings.speech_voice,
                            "input": value, "response_format": "mp3"}}
        limit = 4 * 1024 * 1024
    try:
        start = time.monotonic()
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            with client.stream("POST", settings.speech_base_url.rstrip("/") + path, headers=headers, **kwargs) as response:
                if response.status_code != 200:
                    raise ValueError()
                chunks, size = [], 0
                for chunk in response.iter_bytes(8192):
                    size += len(chunk)
                    if size > limit or time.monotonic() - start > 60:
                        raise ValueError()
                    chunks.append(chunk)
        data = b"".join(chunks)
        if kind == "transcribe":
            text = json.loads(data).get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 8000 or "\x00" in text:
                raise ValueError()
            return {"text": text.strip()}
        if not data:
            raise ValueError()
        return data
    except Exception:
        raise HTTPException(502, "The speech service could not complete this request. Try browser voice.") from None


def run_paid(store, settings, request, authorize, kind, value):
    if not SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Speech services are busy. Try browser voice.")
    lease = "speech-" + token()
    conversation_id = None
    try:
        with store.transaction() as db:
            org_id, subject, conv = authorize(db)
            org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
            check_paid(org, settings)
            if conv:
                if conv.busy_until > time.time():
                    raise HTTPException(409, "Wait for the current request before using voice.")
                conversation_id = conv.id
            # Prepare/validate under the capability before reserving paid usage.
            payload = value(db, conv)
            if kind == "speak" and (not isinstance(payload, str) or not 0 < len(payload) <= MAX_SPEECH_CHARS):
                raise HTTPException(422, "This answer is too long for paid speech. Use browser read-aloud.")
            reserve_speech(db, org, subject, request, settings)
            if conv:
                conv.active_request, conv.busy_until = lease, time.time() + 120
        result = provider_request(settings, kind, payload)
        with store.transaction() as db:
            org_id, _, conv = authorize(db)
            check_paid(db.get(Organization, org_id), settings)
            if conv and (conv.active_request != lease or conv.busy_until <= time.time()):
                raise HTTPException(409, "The speech request expired.")
        return result if kind == "transcribe" else Response(result, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
    finally:
        try:
            if conversation_id:
                with store.transaction() as db:
                    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
                    if conv and conv.active_request == lease:
                        conv.busy_until = 0
        finally:
            SLOTS.release()


def add_public_speech_routes(router, settings, store, authorize_conversation):
    def authorize(db, conversation_id, request):
        conv = authorize_conversation(db, conversation_id, request)
        return db.get(Bot, conv.bot_id).org_id, "chat:" + conv.id, conv

    @router.get("/conversations/{conversation_id}/voice")
    def policy(conversation_id: str, request: Request):
        with store.transaction() as db:
            org_id, _, _ = authorize(db, conversation_id, request)
            return voice_policy(db.get(Organization, org_id), settings)

    @router.post("/conversations/{conversation_id}/transcribe")
    def transcribe(conversation_id: str, request: Request, file: UploadFile = File(...)):
        return run_paid(store, settings, request, lambda db: authorize(db, conversation_id, request),
                        "transcribe", lambda db, conv: normalized_wav(file.file.read(MAX_AUDIO_BYTES + 1)))

    @router.post("/conversations/{conversation_id}/speech")
    def speak(conversation_id: str, request: Request, body: SpeechRequest):
        def answer(db, conv):
            turn = db.get(Turn, (conv.id, body.request_id))
            if not turn or turn.status != "succeeded":
                raise HTTPException(404, "Answer not found.")
            return turn.result["answer"]
        return run_paid(store, settings, request, lambda db: authorize(db, conversation_id, request), "speak", answer)


def add_preview_speech_routes(router, settings, store):
    def authorize(db, bot_id, request):
        session = login_session(db, request, True)
        bot = accessible_bot(db, bot_id, session.user_id)
        return bot.org_id, "author:" + session.user_id, None

    @router.post("/bots/{bot_id}/preview/transcribe")
    def transcribe(bot_id: str, request: Request, file: UploadFile = File(...)):
        return run_paid(store, settings, request, lambda db: authorize(db, bot_id, request),
                        "transcribe", lambda db, conv: normalized_wav(file.file.read(MAX_AUDIO_BYTES + 1)))

    @router.post("/bots/{bot_id}/preview/speech")
    def speak(bot_id: str, request: Request, body: SpeechRequest):
        return run_paid(store, settings, request, lambda db: authorize(db, bot_id, request), "speak", lambda db, conv: body.text)
