import json
import logging
import os
import shutil
import sys
import threading
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory
from flask_sock import Sock
from werkzeug.utils import secure_filename

from services.asr_openai import ASRServiceError, preload_local_whisper, transcribe_audio
from services.asr_stream_deepgram import stream_deepgram
import services.db as db
from services.llm import generate_chat_reply, generate_live_feedback, generate_report_feedback
from services.llm_gemini import LLMServiceError, SYSTEM_PROMPT
from services.pronunciation import aggregate as aggregate_pronunciation
from services.tts_edge import TTSServiceError, synthesize_to_file

load_dotenv(override=True)

LOGGER = logging.getLogger(__name__)

ALLOWED_TTS_VOICES = {
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural",
    "en-AU-NatashaNeural",
    "en-AU-WilliamNeural",
}

ASR_MODEL_OPTIONS = {
    "deepgram:nova-3",
    "deepgram:nova-2",
    "deepgram:nova",
    "deepgram:enhanced",
    "deepgram:base",
    "whisper:tiny.en",
    "whisper:base.en",
    "whisper:small.en",
}

DEFAULT_GREETING = "Hi, I'm ready when you are. Tell me anything in English, and I will help you improve it naturally."


def create_app() -> Flask:
    asr_backend = os.getenv("ASR_BACKEND", "local_whisper")
    asr_model = os.getenv("ASR_MODEL")
    if not asr_model:
        if asr_backend.strip().lower() in {"local_whisper", "whisper_local", "whisper"}:
            asr_model = "tiny.en"
        elif asr_backend.strip().lower() in {"deepgram", "deepgram_api"}:
            asr_model = os.getenv("DEEPGRAM_MODEL", "nova-2")
        else:
            asr_model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me"),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_AUDIO_BYTES", 25 * 1024 * 1024)),
        AUDIO_UPLOAD_DIR=Path(os.getenv("AUDIO_UPLOAD_DIR", "tmp")).resolve(),
        TTS_OUTPUT_DIR=Path(os.getenv("TTS_OUTPUT_DIR", "static/audio")).resolve(),
        ASR_BACKEND=asr_backend,
        ASR_MODEL=asr_model,
        LLM_PROVIDER=os.getenv("LLM_PROVIDER", "gemini"),
        EDGE_TTS_VOICE=os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural"),
        REQUEST_TIMEOUT_SECONDS=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "45")),
        SYNTHESIZE_ON_CHAT=os.getenv("SYNTHESIZE_ON_CHAT", "true").lower() == "true",
        SESSION_STORE={},
        TEMPLATES_AUTO_RELOAD=True,
    )

    app.config["AUDIO_UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["TTS_OUTPUT_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["SOCK"] = Sock(app)
    db.init_db()

    configure_logging(app)
    maybe_preload_whisper(app)
    register_routes(app)
    register_error_handlers(app)
    return app


def configure_logging(app: Flask) -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO))
    app.logger.setLevel(getattr(logging, level_name, logging.INFO))


def maybe_preload_whisper(app: Flask) -> None:
    asr_backend = app.config["ASR_BACKEND"].strip().lower()
    if asr_backend not in {"local_whisper", "whisper_local", "whisper"}:
        return
    if os.getenv("WHISPER_PRELOAD", "false").lower() != "true":
        return
    if "pytest" in Path(sys.argv[0]).name:
        return
    if os.getenv("WERKZEUG_RUN_MAIN") == "false":
        return

    def preload() -> None:
        try:
            app.logger.info("Preloading local Whisper model %s", app.config["ASR_MODEL"])
            preload_local_whisper(
                model=app.config["ASR_MODEL"],
                timeout_seconds=app.config["REQUEST_TIMEOUT_SECONDS"],
            )
            app.logger.info("Local Whisper model is ready")
        except Exception:
            app.logger.exception("Local Whisper preload failed")

    threading.Thread(target=preload, daemon=True).start()


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index() -> str:
        resp = make_response(render_template("index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/health")
    def health() -> Any:
        return jsonify({"ok": True, "timestamp": utc_now_iso()})

    @app.get("/api/settings")
    def api_settings() -> Any:
        return jsonify(
            {
                "default_voice": app.config["EDGE_TTS_VOICE"],
                "default_asr_model": normalize_asr_model(None, app.config["ASR_MODEL"]),
                "default_prompt": SYSTEM_PROMPT,
                "default_greeting": DEFAULT_GREETING,
                "asr_models": [
                    {"value": "deepgram:nova-3", "label": "Deepgram Nova-3"},
                    {"value": "deepgram:nova-2", "label": "Deepgram Nova-2"},
                    {"value": "deepgram:nova", "label": "Deepgram Nova"},
                    {"value": "deepgram:enhanced", "label": "Deepgram Enhanced"},
                    {"value": "deepgram:base", "label": "Deepgram Base"},
                    {"value": "whisper:tiny.en", "label": "Whisper tiny.en"},
                    {"value": "whisper:base.en", "label": "Whisper base.en"},
                    {"value": "whisper:small.en", "label": "Whisper small.en"},
                ],
            }
        )

    @app.get("/audio/<path:filename>")
    def audio_file(filename: str):
        return send_from_directory(app.config["TTS_OUTPUT_DIR"], filename)

    @app.post("/api/chat")
    def api_chat() -> Any:
        upload = request.files.get("audio")
        if upload is None or not upload.filename:
            return error_response("missing_audio", "Please upload an audio file under form field 'audio'.", 400)

        session_id = request.form.get("session_id") or str(uuid.uuid4())
        if is_deleted_session(app, session_id):
            return error_response("session_deleted", "This conversation was deleted.", 410)
        session_state = get_or_create_session(app, session_id)
        asr_model = normalize_asr_model(request.form.get("asr_model"), app.config["ASR_MODEL"])
        system_prompt = normalize_system_prompt(request.form.get("prompt"))
        audio_path = save_upload(app, upload)

        user_audio_url = None
        try:
            asr_result = transcribe_audio(
                audio_path=audio_path,
                model=asr_model,
                timeout_seconds=app.config["REQUEST_TIMEOUT_SECONDS"],
            )
            if isinstance(asr_result, dict):
                transcription = asr_result.get("text", "")
                pronunciation = asr_result.get("pronunciation", {})
            else:
                transcription = str(asr_result)
                pronunciation = {}
            saved_name = f"{uuid.uuid4()}{audio_path.suffix}"
            saved_path = app.config["TTS_OUTPUT_DIR"] / saved_name
            shutil.copy2(audio_path, saved_path)
            user_audio_url = f"/audio/{saved_name}"
            tts_voice = normalize_tts_voice(request.form.get("voice"), app.config["EDGE_TTS_VOICE"])
            response_payload = process_transcript(app, session_id, session_state, transcription, pronunciation, user_audio_url=user_audio_url, tts_voice=tts_voice, system_prompt=system_prompt)
            response_payload["asr_model"] = asr_model
            return jsonify(response_payload)
        except SessionDeletedError as exc:
            return error_response("session_deleted", str(exc), 410)
        except (ASRServiceError, LLMServiceError, TTSServiceError) as exc:
            LOGGER.exception("Pipeline failed")
            return error_response(exc.code, exc.message, exc.status_code)
        except Exception as exc:  # pragma: no cover - defensive fallback
            LOGGER.exception("Unexpected server error")
            return error_response("internal_error", f"Unexpected error: {exc}", 500)
        finally:
            audio_path.unlink(missing_ok=True)

    @app.post("/api/chat_text")
    def api_chat_text() -> Any:
        payload = request.get_json(silent=True) or {}
        transcription = (payload.get("transcription") or "").strip()
        if not transcription:
            return error_response("missing_transcription", "Request JSON must include non-empty 'transcription'.", 400)

        session_id = payload.get("session_id") or str(uuid.uuid4())
        if is_deleted_session(app, session_id):
            return error_response("session_deleted", "This conversation was deleted.", 410)
        session_state = get_or_create_session(app, session_id)
        pronunciation = payload.get("pronunciation") or {}
        tts_voice = normalize_tts_voice(payload.get("voice"), app.config["EDGE_TTS_VOICE"])
        system_prompt = normalize_system_prompt(payload.get("prompt"))

        try:
            return jsonify(process_transcript(app, session_id, session_state, transcription, pronunciation, tts_voice=tts_voice, system_prompt=system_prompt))
        except SessionDeletedError as exc:
            return error_response("session_deleted", str(exc), 410)
        except (LLMServiceError, TTSServiceError) as exc:
            LOGGER.exception("Text chat pipeline failed")
            return error_response(exc.code, exc.message, exc.status_code)
        except Exception as exc:  # pragma: no cover - defensive fallback
            LOGGER.exception("Unexpected server error")
            return error_response("internal_error", f"Unexpected error: {exc}", 500)

    @app.post("/api/speak")
    def api_speak() -> Any:
        payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return error_response("missing_text", "Request JSON must include non-empty 'text'.", 400)
        tts_voice = normalize_tts_voice(payload.get("voice"), app.config["EDGE_TTS_VOICE"])

        try:
            return jsonify({"audio_url": synthesize_reply(app, text, voice=tts_voice), "voice": tts_voice})
        except TTSServiceError as exc:
            LOGGER.exception("TTS failed")
            return error_response(exc.code, exc.message, exc.status_code)

    @app.post("/api/report")
    def api_report() -> Any:
        payload = request.get_json(silent=True) or {}
        session_id = payload.get("session_id")

        if session_id:
            session_state = app.config["SESSION_STORE"].get(session_id)
            if session_state is None:
                return error_response("session_not_found", f"Unknown session_id '{session_id}'.", 404)
        else:
            session_state = normalize_report_payload(payload)

        try:
            feedback = generate_report_feedback(
                session_summary=session_state,
                timeout_seconds=app.config["REQUEST_TIMEOUT_SECONDS"],
            )
        except LLMServiceError:
            feedback = local_report_feedback(session_state)

        summary = build_report_summary(session_state)
        return jsonify({"feedback": feedback, "summary": summary, "session_id": session_id})

    @app.post("/api/live-feedback")
    def api_live_feedback() -> Any:
        payload = request.get_json(silent=True) or {}
        session_id = payload.get("session_id")
        if not session_id:
            return error_response("missing_session_id", "Request JSON must include 'session_id'.", 400)

        session_state = app.config["SESSION_STORE"].get(session_id)
        if session_state is None:
            data = db.get_session_data(session_id)
            if data is None:
                return error_response("session_not_found", f"Unknown session_id '{session_id}'.", 404)
            session_state = data

        messages = session_state.get("messages", [])
        if not messages:
            return error_response("no_messages", "No messages found in this session.", 400)

        try:
            feedback = generate_live_feedback(
                messages=messages,
                timeout_seconds=app.config["REQUEST_TIMEOUT_SECONDS"],
            )
        except Exception:
            LOGGER.exception("Live feedback generation failed")
            feedback = {
                "level": session_state.get("level", "B1"),
                "overall": "Keep practicing — every conversation helps you improve.",
                "strengths": ["You completed a live conversation practice session", "You stayed engaged throughout the session"],
                "improvements": ["Continue working on grammar accuracy", "Try to expand your vocabulary"],
                "tip": "Practice speaking for 10 minutes every day to build fluency and confidence.",
            }

        return jsonify(feedback)

    @app.get("/api/sessions")
    def api_list_sessions() -> Any:
        return jsonify({"sessions": db.list_sessions()})

    @app.post("/api/sessions")
    def api_create_session() -> Any:
        return jsonify(db.create_session()), 201

    @app.get("/api/sessions/<session_id>")
    def api_get_session(session_id: str) -> Any:
        data = db.get_session_data(session_id)
        if data is None:
            return error_response("session_not_found", f"Unknown session_id '{session_id}'.", 404)
        app.config["SESSION_STORE"][session_id] = {
            "session_id": session_id,
            "messages": data["messages"],
            "turns": data["turns"],
            "level": data["level"],
            "corrections": data["corrections"],
            "pronunciation_scores": data["pronunciation_scores"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
        }
        return jsonify(data)

    @app.route("/api/sessions/<session_id>/title", methods=["PATCH"])
    def api_update_title(session_id: str) -> Any:
        payload = request.get_json(silent=True) or {}
        title = (payload.get("title") or "").strip()[:100]
        if not title:
            return error_response("missing_title", "Title cannot be empty.", 400)
        db.set_title(session_id, title)
        return jsonify({"ok": True})

    @app.delete("/api/sessions/<session_id>")
    def api_delete_session(session_id: str) -> Any:
        if not db.delete_session(session_id):
            return error_response("session_not_found", f"Unknown session_id '{session_id}'.", 404)
        app.config["SESSION_STORE"].pop(session_id, None)
        return jsonify({"ok": True})

    @app.config["SOCK"].route("/ws/asr")
    def ws_asr(ws):
        stream_deepgram(ws, timeout_seconds=app.config["REQUEST_TIMEOUT_SECONDS"])


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(413)
    def payload_too_large(_error):
        return error_response("audio_too_large", "Audio file is too large for this server.", 413)


def get_or_create_session(app: Flask, session_id: str) -> dict[str, Any]:
    store = app.config["SESSION_STORE"]
    if session_id not in store:
        store[session_id] = {
            "session_id": session_id,
            "messages": [],
            "turns": 0,
            "level": "B",
            "corrections": [],
            "pronunciation_scores": [],
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
    return store[session_id]


class SessionDeletedError(Exception):
    pass


def is_deleted_session(app: Flask, session_id: str | None) -> bool:
    return bool(session_id and db.is_deleted_session(session_id))


def process_transcript(app: Flask, session_id: str, session_state: dict[str, Any], transcription: str, pronunciation: dict[str, Any], user_audio_url: str | None = None, tts_voice: str | None = None, system_prompt: str | None = None) -> dict[str, Any]:
    llm_result = generate_chat_reply(
        user_text=transcription,
        conversation_history=session_state["messages"],
        timeout_seconds=app.config["REQUEST_TIMEOUT_SECONDS"],
        system_prompt=system_prompt,
    )
    reply_text = llm_result["reply"]

    ai_audio_url = None
    if app.config["SYNTHESIZE_ON_CHAT"] and reply_text:
        ai_audio_url = synthesize_reply(app, reply_text, voice=tts_voice)

    corrections = llm_result.get("corrections", [])
    session_state["messages"].append({"role": "user", "text": transcription})
    session_state["messages"].append({"role": "assistant", "text": reply_text})
    session_state["turns"] += 1
    session_state["level"] = llm_result.get("level") or session_state["level"]
    session_state["corrections"].extend(corrections)
    session_state["pronunciation_scores"].append(pronunciation)
    session_state["updated_at"] = utc_now_iso()

    if is_deleted_session(app, session_id):
        raise SessionDeletedError("This conversation was deleted.")

    db.ensure_session(session_id)
    db.save_turn(session_id, transcription, reply_text, corrections, pronunciation, session_state["level"], user_audio_url=user_audio_url, ai_audio_url=ai_audio_url)
    if session_state["turns"] == 1:
        db.set_title(session_id, transcription[:50])

    return {
        "session_id": session_id,
        "transcription": transcription,
        "reply": reply_text,
        "corrections": corrections,
        "errors": corrections,
        "level": session_state["level"],
        "audio_url": ai_audio_url,
        "voice": tts_voice,
        "user_audio_url": user_audio_url,
        "turns": session_state["turns"],
        "pronunciation": pronunciation,
    }


def save_upload(app: Flask, upload) -> Path:
    suffix = Path(secure_filename(upload.filename or "recording.webm")).suffix or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=app.config["AUDIO_UPLOAD_DIR"]) as temp_file:
        upload.save(temp_file)
        return Path(temp_file.name)


def normalize_asr_model(requested_model: str | None, default_model: str) -> str:
    model = (requested_model or default_model or "whisper:tiny.en").strip()
    if model in {"tiny.en", "base.en", "small.en"}:
        return f"whisper:{model}"
    if model in {"nova-3", "nova-2", "nova", "enhanced", "base"}:
        return f"deepgram:{model}"
    if model in ASR_MODEL_OPTIONS:
        return model
    return default_model if default_model in ASR_MODEL_OPTIONS else "deepgram:nova-2"


def normalize_system_prompt(prompt: str | None) -> str:
    text = (prompt or SYSTEM_PROMPT).strip()
    if not text:
        return SYSTEM_PROMPT
    return text[:8000]


def normalize_tts_voice(requested_voice: str | None, default_voice: str) -> str:
    voice = (requested_voice or default_voice or "en-US-AriaNeural").strip()
    if voice in ALLOWED_TTS_VOICES:
        return voice
    return default_voice if default_voice in ALLOWED_TTS_VOICES else "en-US-AriaNeural"


def synthesize_reply(app: Flask, text: str, voice: str | None = None) -> str:
    tts_voice = normalize_tts_voice(voice, app.config["EDGE_TTS_VOICE"])
    output_name = f"{uuid.uuid4()}.mp3"
    output_path = app.config["TTS_OUTPUT_DIR"] / output_name
    synthesize_to_file(
        text=text,
        output_path=output_path,
        voice=tts_voice,
        timeout_seconds=app.config["REQUEST_TIMEOUT_SECONDS"],
    )
    return f"/audio/{output_name}"


def normalize_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    corrections = payload.get("corrections") or payload.get("errors") or []
    messages = payload.get("messages") or []
    level = payload.get("level") or "B"
    turns = payload.get("turns")
    if turns is None:
        turns = sum(1 for item in messages if item.get("role") == "user")
    return {
        "messages": messages,
        "turns": turns or 0,
        "level": level,
        "corrections": corrections,
        "pronunciation_scores": payload.get("pronunciation_scores") or [],
    }


def build_report_summary(session_state: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(classify_error(item.get("type", "other")) for item in session_state.get("corrections", []))
    pron_agg = aggregate_pronunciation(session_state.get("pronunciation_scores", []))
    return {
        "turns": session_state.get("turns", 0),
        "level": session_state.get("level", "B"),
        "total_errors": sum(counts.values()),
        "error_breakdown": {
            "tense": counts.get("tense", 0),
            "preposition": counts.get("preposition", 0),
            "article": counts.get("article", 0),
            "other": counts.get("other", 0),
        },
        "pronunciation": pron_agg,
    }


def local_report_feedback(session_state: dict[str, Any]) -> str:
    summary = build_report_summary(session_state)
    breakdown = summary["error_breakdown"]
    if summary["turns"] == 0:
        return "Start one short conversation first, then I can generate a useful practice report."

    dominant_type = max(breakdown, key=breakdown.get) if summary["total_errors"] else "fluency"
    if summary["total_errors"] == 0:
        return "Nice work. This session was very clean grammatically, so the next step is speaking longer and more naturally."

    return (
        f"You completed {summary['turns']} turns at about {summary['level']} level. "
        f"Your most frequent issue was {dominant_type}. Focus on short self-corrections after each sentence to reduce repeat mistakes."
    )


def classify_error(error_type: str) -> str:
    lowered = error_type.lower()
    if "tense" in lowered:
        return "tense"
    if "prep" in lowered:
        return "preposition"
    if "article" in lowered:
        return "article"
    return "other"


def error_response(code: str, message: str, status_code: int) -> Any:
    return jsonify({"error": {"code": code, "message": message}}), status_code


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


app = create_app()


class _StripWsExtensions:
    """Strip Sec-WebSocket-Extensions so simple_websocket doesn't negotiate
    permessage-deflate, which it implements incorrectly and causes Chrome to
    reject frames with 'Invalid frame header'."""
    def __init__(self, wsgi_app):
        self._app = wsgi_app

    def __call__(self, environ, start_response):
        environ.pop("HTTP_SEC_WEBSOCKET_EXTENSIONS", None)
        return self._app(environ, start_response)


app.wsgi_app = _StripWsExtensions(app.wsgi_app)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
        use_reloader=False,
        threaded=True,
    )
