from io import BytesIO

import pytest

from app import create_app
from services.llm_gemini import LLMServiceError
from services.asr_openai import _parse_deepgram_result
from services.asr_stream_deepgram import _deepgram_stream_url


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEAKAI_DB_PATH", str(tmp_path / "speakai-test.db"))
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json["ok"] is True


def test_chat_requires_audio(client):
    response = client.post("/api/chat", data={})
    assert response.status_code == 400
    assert response.json["error"]["code"] == "missing_audio"


def test_chat_pipeline_success(client, monkeypatch):
    client.application.config["SYNTHESIZE_ON_CHAT"] = True
    monkeypatch.setattr("app.transcribe_audio", lambda **kwargs: "I goes to school yesterday.")
    monkeypatch.setattr(
        "app.generate_chat_reply",
        lambda **kwargs: {
            "reply": "You can say, I went to school yesterday. What did you study there?",
            "level": "B",
            "corrections": [
                {
                    "type": "tense",
                    "original": "goes",
                    "corrected": "went",
                    "reason": "Past time marker yesterday needs past tense.",
                }
            ],
        },
    )
    monkeypatch.setattr("app.synthesize_reply", lambda app, text, voice=None: "/audio/fake.mp3")

    response = client.post(
        "/api/chat",
        data={"audio": (BytesIO(b"fake-webm"), "recording.webm")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.json["transcription"] == "I goes to school yesterday."
    assert response.json["reply"].startswith("You can say")
    assert response.json["audio_url"] == "/audio/fake.mp3"
    assert response.json["corrections"][0]["type"] == "tense"


def test_chat_fast_mode_skips_tts(client, monkeypatch):
    client.application.config["SYNTHESIZE_ON_CHAT"] = False
    monkeypatch.setattr("app.transcribe_audio", lambda **kwargs: "I went to school yesterday.")
    monkeypatch.setattr(
        "app.generate_chat_reply",
        lambda **kwargs: {
            "reply": "Great. Tell me one thing you studied.",
            "level": "B",
            "corrections": [],
        },
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("TTS should not run in fast chat mode.")

    monkeypatch.setattr("app.synthesize_reply", fail_if_called)

    response = client.post(
        "/api/chat",
        data={"audio": (BytesIO(b"fake-webm"), "recording.webm")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.json["reply"].startswith("Great")
    assert response.json["audio_url"] is None


def test_chat_uses_requested_asr_model(client, monkeypatch):
    seen = {}

    def fake_transcribe(**kwargs):
        seen["model"] = kwargs["model"]
        return "I went to school yesterday."

    monkeypatch.setattr("app.transcribe_audio", fake_transcribe)
    monkeypatch.setattr(
        "app.generate_chat_reply",
        lambda **kwargs: {
            "reply": "Nice. What was your favorite class?",
            "level": "B",
            "corrections": [],
        },
    )

    response = client.post(
        "/api/chat",
        data={"audio": (BytesIO(b"fake-webm"), "recording.webm"), "asr_model": "small.en"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert seen["model"] == "whisper:small.en"
    assert response.json["asr_model"] == "whisper:small.en"


def test_chat_uses_requested_prompt(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("app.transcribe_audio", lambda **kwargs: "Hello.")

    def fake_reply(**kwargs):
        seen["system_prompt"] = kwargs["system_prompt"]
        return {"reply": "Hi.", "level": "B", "corrections": []}

    monkeypatch.setattr("app.generate_chat_reply", fake_reply)

    response = client.post(
        "/api/chat",
        data={
            "audio": (BytesIO(b"fake-webm"), "recording.webm"),
            "prompt": "You are a strict IELTS coach.",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert seen["system_prompt"] == "You are a strict IELTS coach."


def test_settings_exposes_defaults(client):
    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json["default_prompt"]
    assert response.json["default_greeting"]
    assert any(item["value"] == "deepgram:nova-2" for item in response.json["asr_models"])
    assert any(item["value"] == "whisper:small.en" for item in response.json["asr_models"])


def test_chat_uses_requested_tts_voice(client, monkeypatch):
    client.application.config["SYNTHESIZE_ON_CHAT"] = True
    seen = {}
    monkeypatch.setattr("app.transcribe_audio", lambda **kwargs: "I went to school yesterday.")
    monkeypatch.setattr(
        "app.generate_chat_reply",
        lambda **kwargs: {
            "reply": "Nice. What did you study?",
            "level": "B",
            "corrections": [],
        },
    )

    def fake_synthesize(_app, _text, voice=None):
        seen["voice"] = voice
        return "/audio/fake.mp3"

    monkeypatch.setattr("app.synthesize_reply", fake_synthesize)

    response = client.post(
        "/api/chat",
        data={
            "audio": (BytesIO(b"fake-webm"), "recording.webm"),
            "voice": "en-GB-RyanNeural",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert seen["voice"] == "en-GB-RyanNeural"
    assert response.json["voice"] == "en-GB-RyanNeural"


def test_speak_rejects_unknown_voice_to_default(client, monkeypatch):
    seen = {}

    def fake_synthesize_to_file(**kwargs):
        seen["voice"] = kwargs["voice"]

    monkeypatch.setattr("app.synthesize_to_file", fake_synthesize_to_file)

    response = client.post("/api/speak", json={"text": "hello", "voice": "unknown"})

    assert response.status_code == 200
    assert seen["voice"] == client.application.config["EDGE_TTS_VOICE"]
    assert response.json["voice"] == client.application.config["EDGE_TTS_VOICE"]


def test_deleted_session_cannot_be_recreated_by_late_chat_text(client, monkeypatch):
    session_id = "deleted-session-id"
    import services.db as db

    db.delete_session(session_id)
    monkeypatch.setattr(
        "app.generate_chat_reply",
        lambda **kwargs: {
            "reply": "This should not be saved.",
            "level": "B",
            "corrections": [],
        },
    )

    response = client.post(
        "/api/chat_text",
        json={"session_id": session_id, "transcription": "hello"},
    )

    assert response.status_code == 410
    assert response.json["error"]["code"] == "session_deleted"


def test_chat_returns_quota_error_when_gemini_quota_exceeded(client, monkeypatch):
    client.application.config["SYNTHESIZE_ON_CHAT"] = False
    monkeypatch.setattr(
        "app.transcribe_audio",
        lambda **kwargs: {"text": "I went to school.", "pronunciation": {"score": 80, "filler_count": 0}},
    )

    def quota_error(**_kwargs):
        raise LLMServiceError(
            "Gemini quota exceeded for the current model/API key. Try again later.",
            code="gemini_quota_exceeded",
            status_code=429,
        )

    monkeypatch.setattr("app.generate_chat_reply", quota_error)

    response = client.post(
        "/api/chat",
        data={"audio": (BytesIO(b"fake-webm"), "recording.webm")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 429
    assert response.json["error"]["code"] == "gemini_quota_exceeded"
    assert "quota exceeded" in response.json["error"]["message"].lower()


def test_speak_requires_text(client):
    response = client.post("/api/speak", json={})
    assert response.status_code == 400
    assert response.json["error"]["code"] == "missing_text"


def test_parse_deepgram_result():
    text, words, duration, confidence = _parse_deepgram_result(
        {
            "metadata": {"duration": 2.5},
            "results": {
                "channels": [
                    {
                        "alternatives": [
                            {
                                "transcript": "hello world",
                                "confidence": 0.91,
                                "words": [
                                    {"word": "hello", "start": 0.1, "end": 0.5, "confidence": 0.9},
                                    {"word": "world", "start": 0.7, "end": 1.1, "confidence": 0.92},
                                ],
                            }
                        ]
                    }
                ]
            },
        }
    )

    assert text == "hello world"
    assert len(words) == 2
    assert duration == 2.5
    assert confidence == 0.91


def test_deepgram_stream_url_lets_browser_webm_autodetect(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_STREAM_ENCODING", raising=False)
    monkeypatch.delenv("DEEPGRAM_STREAM_SAMPLE_RATE", raising=False)

    url = _deepgram_stream_url()

    assert "interim_results=true" in url
    assert "no_delay=true" in url
    assert "encoding=" not in url
    assert "sample_rate=" not in url


def test_report_with_payload_fallback(client, monkeypatch):
    monkeypatch.setattr("app.generate_report_feedback", lambda **kwargs: "Focus on tense consistency and keep extending your answers.")
    response = client.post(
        "/api/report",
        json={
            "turns": 2,
            "level": "B",
            "corrections": [{"type": "tense"}, {"type": "article"}],
            "messages": [{"role": "user", "text": "hello"}],
        },
    )
    assert response.status_code == 200
    assert response.json["summary"]["total_errors"] == 2
    assert "tense" in response.json["summary"]["error_breakdown"]


def test_report_includes_pronunciation_summary(client, monkeypatch):
    monkeypatch.setattr("app.generate_report_feedback", lambda **kwargs: "Keep your pace steady and reduce fillers.")
    response = client.post(
        "/api/report",
        json={
            "turns": 2,
            "level": "B",
            "corrections": [],
            "messages": [{"role": "user", "text": "hello"}],
            "pronunciation_scores": [
                {"score": 70, "speaking_rate_wpm": 110, "filler_count": 1, "pause_frequency_per_min": 2, "volume_stability": 80},
                {"score": 90, "speaking_rate_wpm": 130, "filler_count": 0, "pause_frequency_per_min": 4, "volume_stability": 90},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json["summary"]["pronunciation"]["avg_score"] == 80
    assert response.json["summary"]["pronunciation"]["avg_speaking_rate_wpm"] == 120
    assert response.json["summary"]["pronunciation"]["total_filler_count"] == 1
    assert response.json["summary"]["pronunciation"]["avg_pause_frequency_per_min"] == 3
    assert response.json["summary"]["pronunciation"]["avg_volume_stability"] == 85
