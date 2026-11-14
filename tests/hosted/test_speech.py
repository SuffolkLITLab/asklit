import io
import wave

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from asklit.hosted.models import Conversation, Organization, RateBucket, Turn
from asklit.hosted.speech import normalized_wav, provider_request
from test_api import new_bot, publish


def recording(seconds=1, rate=16000, channels=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x01\x00" * int(seconds * rate) * channels)
    return out.getvalue()


def setup(hosted, paid=False, limit=100, voice=("service", "service", "neutral"), providers=("default", "default")):
    """voice=None leaves the instructor's voice settings at their defaults (off)."""
    settings, store = hosted[:2]
    settings.speech_api_key = "test-only-key"
    settings.speech_stt_model = "tiny-test-transcribe"
    settings.speech_tts_model = "tiny-test-tts"
    bot = new_bot(hosted)
    if voice:
        listen, speak, persona = voice
        saved = hosted[4]("alice").put(f"/api/bots/{bot['id']}", json={"version": bot["version"], "config": {
            **bot["config"], "voice_enabled": True, "voice_listen": listen, "voice_speak": speak, "voice_persona": persona,
            "voice_listen_provider": providers[0], "voice_speak_provider": providers[1]}})
        assert saved.status_code == 200, saved.text
        bot = saved.json()
    publish(hosted, bot)
    with store.transaction() as db:
        org = db.get(Organization, "school")
        org.paid_voice_enabled = paid
        org.paid_voice_daily_calls = limit
    conversation = hosted[3].post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    return bot, conversation, {"Authorization": "Bearer " + conversation["token"]}


def transcribe(hosted, conv, headers, data=None):
    return hosted[3].post(f"/api/public/conversations/{conv['id']}/transcribe",
                          files={"file": ("speech.wav", data if data is not None else recording())}, headers=headers)


def test_browser_always_available_and_paid_requires_admin(hosted):
    bot, conv, headers = setup(hosted)
    assert hosted[3].get(f"/api/public/bots/{bot['id']}").json()["voice"] == {
        "browser": True, "paid": False, "paid_stt": False, "paid_tts": False,
        "max_recording_seconds": 30, "max_speech_characters": 4000, "locked": True, "enabled": True,
        "persona": "neutral", "listen_mode": "browser", "speak_mode": "browser"}
    assert transcribe(hosted, conv, headers).status_code == 403
    assert hosted[3].put("/api/organizations/school/sharing", json={"slug": "school", "paid_voice_enabled": True}).status_code == 404
    owner = hosted[4]("owner")
    saved = owner.put("/api/organizations/school/sharing", json={"slug": "school", "paid_voice_enabled": True, "paid_voice_daily_calls": 12})
    assert saved.status_code == 200
    assert saved.json()["paid_voice_daily_calls"] == 12
    assert hosted[3].get(f"/api/public/bots/{bot['id']}").json()["voice"]["paid"]
    hosted[0].speech_api_key = ""
    assert not hosted[3].get(f"/api/public/bots/{bot['id']}").json()["voice"]["paid"]
    unchanged = owner.put("/api/organizations/school/sharing", json={"slug": "school"})
    assert unchanged.status_code == 200
    assert unchanged.json()["paid_voice_enabled"] is True
    assert unchanged.json()["paid_voice_daily_calls"] == 12
    assert owner.put("/api/organizations/school/sharing", json={"slug": "school", "paid_voice_enabled": False}).status_code == 200
    assert owner.put("/api/organizations/school/sharing", json={"slug": "school", "paid_voice_enabled": True}).status_code == 422


def test_paid_quota_and_capabilities(hosted, monkeypatch):
    bot, conv, headers = setup(hosted, paid=True, limit=1)
    calls = []
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda settings, kind, value: calls.append(kind) or {"text": "When is the deadline?"})
    assert transcribe(hosted, conv, {}).status_code == 404
    assert hosted[3].get(f"/api/public/conversations/{conv['id']}/transcribe", headers=headers).status_code == 405
    assert transcribe(hosted, conv, headers).json()["text"] == "When is the deadline?"
    assert calls == ["transcribe"]
    assert transcribe(hosted, conv, headers).status_code == 429
    assert calls == ["transcribe"]
    # The speech cap never disables normal text or browser voice.
    assert hosted[3].post(f"/api/public/conversations/{conv['id']}/messages",
        headers=headers, json={"message": "When?", "request_id": "normal"}).status_code == 200


def test_tts_only_reads_a_stored_answer_and_rechecks_disable(hosted, monkeypatch):
    _, conv, headers = setup(hosted, paid=True)
    path = f"/api/public/conversations/{conv['id']}"
    assert hosted[3].post(path + "/speech", headers=headers, json={"text": "arbitrary narration"}).status_code == 404
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Question", "request_id": "answer"})
    captured = []
    def synth(settings, kind, value):
        captured.append((kind, value))
        return b"ID3-test-audio"
    monkeypatch.setattr("asklit.hosted.speech.provider_request", synth)
    result = hosted[3].post(path + "/speech", headers=headers, json={"request_id": "answer", "text": "ignored"})
    assert result.status_code == 200
    assert result.content == b"ID3-test-audio"
    assert captured == [("speak", "Grounded answer")]
    assert result.headers["content-type"] == "audio/mpeg"
    def disable(settings, kind, value):
        with hosted[1].transaction() as db:
            db.get(Organization, "school").paid_voice_enabled = False
        return b"must-not-be-released"
    monkeypatch.setattr("asklit.hosted.speech.provider_request", disable)
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "answer"}).status_code == 403
    with hosted[1].transaction() as db:
        assert db.get(Conversation, conv["id"]).busy_until == 0


@pytest.mark.parametrize("data", [b"not wav", recording(31), recording(rate=8000), recording(channels=2), b"x" * (1024 * 1024 + 1)])
def test_audio_limits_precede_paid_calls(hosted, monkeypatch, data):
    _, conv, headers = setup(hosted, paid=True)
    def forbidden(*args):
        pytest.fail("Invalid audio must not reach the provider.")
    monkeypatch.setattr("asklit.hosted.speech.provider_request", forbidden)
    assert transcribe(hosted, conv, headers, data).status_code in {413, 422}
    with hosted[1].transaction() as db:
        assert not list(db.scalars(select(RateBucket).where(RateBucket.key.like("speech:%"))))


def test_normalizes_wav_and_discards_trailing_content():
    original = recording()
    normalized = normalized_wav(original + b"trailing-content")
    assert normalized == original


def test_failed_paid_call_counts_and_releases_conversation(hosted, monkeypatch):
    _, conv, headers = setup(hosted, paid=True, limit=1)
    def fail(*args):
        raise HTTPException(502, "Speech service unavailable")
    monkeypatch.setattr("asklit.hosted.speech.provider_request", fail)
    assert transcribe(hosted, conv, headers).status_code == 502
    with hosted[1].transaction() as db:
        assert db.get(Conversation, conv["id"]).busy_until == 0
    assert transcribe(hosted, conv, headers).status_code == 429


def test_preview_paid_voice_uses_csrf_and_organization_policy(hosted, monkeypatch):
    bot, _, _ = setup(hosted, paid=True)
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda settings, kind, value: {"text": "Preview"} if kind == "transcribe" else b"ID3-audio")
    c = hosted[3]
    assert c.post(f"/api/bots/{bot['id']}/preview/transcribe", files={"file":("speech.wav", recording())}).json() == {"text": "Preview"}
    assert c.post(f"/api/bots/{bot['id']}/preview/speech", json={"text":"Answer"}).content == b"ID3-audio"
    del c.headers["x-csrf-token"]
    assert c.post(f"/api/bots/{bot['id']}/preview/speech", json={"text":"Answer"}).status_code == 403


def test_provider_adapter_bounded_and_no_error_echo(hosted, monkeypatch):
    import httpx
    settings = hosted[0]
    settings.speech_api_key = "test-only-secret"
    settings.speech_stt_model = "tiny-test-transcribe"
    settings.speech_tts_model = "tiny-test-tts"
    original = httpx.Client
    seen = []
    def handle(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer test-only-secret"
        return httpx.Response(200, json={"text":"Transcript"}) if request.url.path.endswith("transcriptions") else httpx.Response(200, content=b"ID3-audio")
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr("asklit.hosted.speech.httpx.Client", lambda **kwargs: original(transport=transport, **kwargs))
    assert provider_request(settings, "transcribe", recording()) == {"text":"Transcript"}
    assert provider_request(settings, "speak", "Answer") == b"ID3-audio"
    assert b"tiny-test-transcribe" in seen[0].content
    transport.handler = lambda request: httpx.Response(401, text="test-only-secret")
    with pytest.raises(HTTPException) as exc:
        provider_request(settings, "speak", "Answer")
    assert "test-only-secret" not in exc.value.detail


def aws_setup(hosted, budget=None):
    settings = hosted[0]
    settings.speech_provider = "aws"
    settings.monthly_ai_budget_usd = budget
    return setup(hosted, paid=True)


def test_aws_speech_is_metered_in_dollars(hosted, monkeypatch):
    from asklit.hosted.models import UsageEvent
    from asklit.hosted.budget import budget_report
    hosted[0].speech_api_key = ""  # AWS credentials come from the environment, not a speech key
    bot, conv, headers = aws_setup(hosted)
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda settings, kind, value: {"text": "Hi"} if kind == "transcribe" else b"ID3")
    assert transcribe(hosted, conv, headers, recording(2)).status_code == 200  # billed at the 15-second minimum
    path = f"/api/public/conversations/{conv['id']}"
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Q", "request_id": "answer"})
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "answer"}).status_code == 200
    with hosted[1].transaction() as db:
        events = {e.purpose: e for e in db.scalars(select(UsageEvent).where(UsageEvent.purpose.like("speech-%")))}
        assert events["speech-transcribe"].status == "succeeded"
        assert events["speech-transcribe"].estimated_cost_usd == pytest.approx(15 / 60 * 0.024)
        assert events["speech-speak"].estimated_cost_usd == pytest.approx(len("Grounded answer") * 16 / 1_000_000)


def test_aws_speech_respects_monthly_budget_and_keeps_failed_reservation(hosted, monkeypatch):
    from asklit.hosted.models import UsageEvent
    bot, conv, headers = aws_setup(hosted, budget=0.013)  # room for two 15 s transcriptions ($0.006 each), not three
    attempts = []
    def provider(settings, kind, value):
        attempts.append(kind)
        if len(attempts) == 1:
            raise HTTPException(502, "down")
        return {"text": "ok"}
    monkeypatch.setattr("asklit.hosted.speech.provider_request", provider)
    assert transcribe(hosted, conv, headers).status_code == 502  # failed call still holds $0.006
    assert transcribe(hosted, conv, headers).status_code == 200
    assert transcribe(hosted, conv, headers).status_code == 429  # budget exhausted before the provider is called
    assert attempts == ["transcribe", "transcribe"]
    with hosted[1].transaction() as db:
        statuses = sorted(e.status for e in db.scalars(select(UsageEvent).where(UsageEvent.purpose == "speech-transcribe")))
        assert statuses == ["failed", "succeeded"]


def test_compatible_speech_is_metered_under_a_budget(hosted, monkeypatch):
    from asklit.hosted.models import UsageEvent
    settings = hosted[0]
    settings.monthly_ai_budget_usd = 100
    bot, conv, headers = setup(hosted, paid=True)
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda s, kind, value: {"text": "Hi"} if kind == "transcribe" else b"ID3")
    assert transcribe(hosted, conv, headers, recording(2)).status_code == 200
    with hosted[1].transaction() as db:
        event = db.scalar(select(UsageEvent).where(UsageEvent.purpose == "speech-transcribe"))
        assert (event.preset, event.model, event.status) == ("compatible-stt", "tiny-test-transcribe", "succeeded")
        assert event.estimated_cost_usd == pytest.approx(2 / 60 * 0.006)


def test_each_direction_can_use_a_different_provider(hosted, monkeypatch):
    from asklit.hosted.speech import provider_for, speech_cost, voice_policy
    settings = hosted[0]
    settings.speech_api_key = "k"
    settings.speech_stt_provider, settings.speech_tts_provider = "aws", "compatible"
    settings.speech_stt_model, settings.speech_tts_model = "", "gpt-4o-mini-tts"
    assert (provider_for(settings, "transcribe"), provider_for(settings, "speak")) == ("aws", "compatible")
    assert speech_cost(settings, "speak", "x" * 1000) == pytest.approx(1000 * 30 / 1_000_000)
    assert speech_cost(settings, "transcribe", b"\0" * 44 + b"\0" * 32000) == pytest.approx(15 / 60 * 0.024)
    settings.speech_tts_model = ""  # OpenAI-style TTS not configured, Transcribe still is
    org = type("Org", (), {"paid_voice_enabled": True})()
    assert voice_policy(org, settings)["paid_stt"] and not voice_policy(org, settings)["paid_tts"]
    assert not voice_policy(org, settings)["paid"]
    settings.speech_tts_model = "gpt-4o-mini-tts"
    policy = voice_policy(org, settings)
    assert policy["paid_stt"] and policy["paid_tts"] and policy["paid"]


def test_tts_only_provider_rejects_transcribe_but_speaks(hosted, monkeypatch):
    settings = hosted[0]
    settings.speech_stt_model = ""  # only OpenAI TTS is configured; speech-to-text stays in the browser
    bot, conv, headers = setup(hosted, paid=True)
    settings.speech_stt_model = ""
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda s, k, v: b"ID3")
    assert transcribe(hosted, conv, headers).status_code == 503
    path = f"/api/public/conversations/{conv['id']}"
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Q", "request_id": "a"})
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "a"}).status_code == 200


def test_aws_caps_speech_at_polly_limit_before_reserving(hosted, monkeypatch):
    from asklit.hosted.models import UsageEvent
    _, conv, headers = aws_setup(hosted)
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda *a: pytest.fail("must not reach Polly"))
    path = f"/api/public/conversations/{conv['id']}"
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Q", "request_id": "long"})
    with hosted[1].transaction() as db:
        db.get(Turn, (conv["id"], "long")).result = {"answer": "x" * 3001}
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "long"}).status_code == 422
    with hosted[1].transaction() as db:
        assert not list(db.scalars(select(UsageEvent).where(UsageEvent.purpose == "speech-speak")))
        assert not list(db.scalars(select(RateBucket).where(RateBucket.key.like("speech:%"))))


def test_nonfinite_speech_rates_are_rejected():
    from asklit.hosted.settings import Settings
    for field in ("speech_stt_usd_per_minute", "speech_tts_usd_per_million_chars"):
        for bad in (float("nan"), float("inf"), 0.0, -1.0):
            with pytest.raises(ValueError):
                Settings(database_url="sqlite://", secret_key="x" * 40, **{field: bad})


@pytest.mark.parametrize("text,expected", [
    ("According to Dossier.pdf, page 3, you can appeal within 60 days.", "you can appeal within 60 days."),
    ("You have 60 days [1] to appeal (Source 2).", "You have 60 days to appeal."),
    ("Per the file handbook.docx p. 4, the limit is $2,000.", "the limit is $2,000."),
    ("Bring your notice, see Sources 1 and 2.", "Bring your notice."),
    ("Nothing to strip here, version 2.0 is fine.", "Nothing to strip here, version 2.0 is fine."),
])
def test_spoken_text_drops_source_mentions_only(text, expected):
    from asklit.hosted.speech import spoken_text
    assert spoken_text(text, [{"filename": "Dossier.pdf"}]) == expected


def test_aws_read_aloud_never_sends_source_mentions(hosted, monkeypatch):
    _, conv, headers = aws_setup(hosted)
    path = f"/api/public/conversations/{conv['id']}"
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Q", "request_id": "cited"})
    with hosted[1].transaction() as db:
        db.get(Turn, (conv["id"], "cited")).result = {
            "answer": "According to Guide.pdf, page 2, file by Friday [1]. Source 3 agrees.",
            "sources": [{"filename": "Guide.pdf", "page": 2, "content": "x"}]}
    seen = []
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda settings, kind, value: seen.append(value) or b"ID3")
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "cited"}).status_code == 200
    assert seen == ["file by Friday. agrees."] or all(w not in seen[0] for w in ("Guide.pdf", "[1]", "Source"))


def test_browser_voice_needs_no_opt_in_but_paid_voice_does(hosted, monkeypatch):
    bot, conv, headers = setup(hosted, paid=True, voice=None)  # instructor never touched the voice settings
    policy = hosted[3].get(f"/api/public/bots/{bot['id']}").json()["voice"]
    assert policy["enabled"] is True and policy["locked"] is True
    assert (policy["listen_mode"], policy["speak_mode"]) == ("browser", "browser")
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda *a: pytest.fail("paid voice was never chosen"))
    assert transcribe(hosted, conv, headers).status_code == 403


def test_instructor_can_turn_voice_off(hosted):
    bot, _, _ = setup(hosted, paid=True)
    c = hosted[4]("alice")
    current = c.get(f"/api/bots/{bot['id']}").json()
    assert c.put(f"/api/bots/{bot['id']}", json={"version": current["version"], "config": {**current["config"], "voice_enabled": False}}).status_code == 200
    assert c.post(f"/api/bots/{bot['id']}/publish", json={"version": c.get(f"/api/bots/{bot['id']}").json()["version"]}).status_code == 200
    assert hosted[3].get(f"/api/public/bots/{bot['id']}").json()["voice"]["enabled"] is False


def test_instructor_chooses_the_kind_of_voice_and_students_cannot_override(hosted, monkeypatch):
    bot, conv, headers = setup(hosted, paid=True, voice=("browser", "service", "neutral"))
    policy = hosted[3].get(f"/api/public/bots/{bot['id']}").json()["voice"]
    assert (policy["listen_mode"], policy["speak_mode"]) == ("browser", "service")
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda *a: pytest.fail("transcription is browser-only"))
    assert transcribe(hosted, conv, headers).status_code == 403  # a student calling the endpoint directly
    assert hosted[3].get(f"/api/public/conversations/{conv['id']}/voice", headers=headers).json()["listen_mode"] == "browser"
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda s, k, v: b"ID3")
    path = f"/api/public/conversations/{conv['id']}"
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Q", "request_id": "r"})
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "r"}).status_code == 200


@pytest.mark.parametrize("persona,openai,polly", [("feminine", "nova", "Joanna"), ("masculine", "echo", "Matthew"), ("neutral", "alloy", "Ivy")])
def test_voice_persona_selects_the_provider_voice(hosted, monkeypatch, persona, openai, polly):
    hosted[0].speech_voice, hosted[0].speech_aws_voice = "alloy", "Ivy"
    _, conv, headers = setup(hosted, paid=True, voice=("browser", "service", persona))
    seen = []
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda s, k, v: seen.append((s.speech_voice, s.speech_aws_voice)) or b"ID3")
    path = f"/api/public/conversations/{conv['id']}"
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Q", "request_id": "r"})
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "r"}).status_code == 200
    assert seen == [(openai, polly)] if persona != "neutral" else seen == [("alloy", "Ivy")]


def test_author_preview_ignores_the_published_lock_but_uses_the_draft_persona(hosted):
    bot, _, _ = setup(hosted, paid=True, voice=("browser", "browser", "masculine"))
    policy = hosted[3].get(f"/api/bots/{bot['id']}/voice").json()
    assert policy["persona"] == "masculine" and "locked" not in policy


def test_author_chooses_openai_or_polly_per_direction(hosted, monkeypatch):
    from asklit.hosted.models import UsageEvent
    hosted[0].speech_provider = "compatible"  # operator default: OpenAI-style
    _, conv, headers = setup(hosted, paid=True, voice=("service", "service", "neutral"), providers=("aws", "aws"))
    seen = []
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda s, k, v: seen.append((k, s.speech_stt_provider, s.speech_tts_provider)) or ({"text": "Hi"} if k == "transcribe" else b"ID3"))
    assert transcribe(hosted, conv, headers, recording(2)).status_code == 200
    path = f"/api/public/conversations/{conv['id']}"
    hosted[3].post(path + "/messages", headers=headers, json={"message": "Q", "request_id": "r"})
    assert hosted[3].post(path + "/speech", headers=headers, json={"request_id": "r"}).status_code == 200
    assert seen == [("transcribe", "aws", ""), ("speak", "", "aws")] or all("aws" in row for row in seen)
    with hosted[1].transaction() as db:
        presets = {e.purpose: e.preset for e in db.scalars(select(UsageEvent).where(UsageEvent.purpose.like("speech-%")))}
    assert presets == {"speech-transcribe": "aws-transcribe", "speech-speak": "aws-polly"}


def test_an_unconfigured_provider_choice_fails_safe(hosted, monkeypatch):
    hosted[0].speech_provider = "aws"
    _, conv, headers = setup(hosted, paid=True, voice=("service", "service", "neutral"), providers=("openai", "openai"))
    hosted[0].speech_api_key = ""  # operator never set up OpenAI
    monkeypatch.setattr("asklit.hosted.speech.provider_request", lambda *a: pytest.fail("provider is not configured"))
    assert transcribe(hosted, conv, headers).status_code == 503
    policy = hosted[3].get(f"/api/public/conversations/{conv['id']}/voice", headers=headers).json()
    assert policy["paid_stt"] is False and policy["speak_mode"] == "browser"


def test_preview_policy_lists_the_providers_an_author_can_pick(hosted):
    hosted[0].speech_provider = "aws"
    bot, _, _ = setup(hosted, paid=True)
    hosted[0].speech_api_key = ""  # setup() configures an OpenAI-style key; remove it
    providers = hosted[3].get(f"/api/bots/{bot['id']}/voice").json()["providers"]
    assert providers == {"listen": ["aws"], "speak": ["aws"]}  # no OpenAI key configured
    hosted[0].speech_api_key, hosted[0].speech_tts_model, hosted[0].speech_stt_model = "k", "m", "m"
    assert hosted[3].get(f"/api/bots/{bot['id']}/voice").json()["providers"] == {"listen": ["openai", "aws"], "speak": ["openai", "aws"]}
