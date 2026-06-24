import json
import os
import threading
import time
from urllib.parse import urlencode

from services.asr_openai import ASRServiceError
from services.pronunciation import analyze_from_words


def stream_deepgram(client_ws, timeout_seconds: float) -> None:
    import sys
    print("[ASR] stream_deepgram called", flush=True)
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        print("[ASR] ERROR: missing DEEPGRAM_API_KEY", flush=True)
        client_ws.send(json.dumps({"type": "error", "code": "missing_deepgram_key", "message": "DEEPGRAM_API_KEY is not configured."}))
        return

    try:
        import websocket
    except ImportError:
        print("[ASR] ERROR: websocket-client not installed", flush=True)
        client_ws.send(json.dumps({"type": "error", "code": "missing_websocket_client", "message": "websocket-client package is not installed."}))
        return

    deepgram_ws = None
    closed = threading.Event()
    final_parts = []
    final_words = []
    latest_duration = 0.0

    try:
        url = _deepgram_stream_url()
        print(f"[ASR] Connecting to Deepgram: {url}", flush=True)
        deepgram_ws = websocket.create_connection(
            url,
            header=[f"Authorization: Token {api_key}"],
            timeout=timeout_seconds,
        )
        print("[ASR] Deepgram connected OK", flush=True)
    except Exception as exc:
        print(f"[ASR] ERROR connecting to Deepgram: {exc}", flush=True)
        client_ws.send(json.dumps({"type": "error", "code": "deepgram_stream_connect_failed", "message": f"Could not connect to Deepgram streaming ASR: {exc}"}))
        return

    def receive_deepgram() -> None:
        nonlocal latest_duration
        while not closed.is_set():
            try:
                message = deepgram_ws.recv()
            except Exception:
                break
            if not message:
                continue
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            transcript, words, duration, confidence = _parse_stream_result(payload)
            if duration:
                latest_duration = max(latest_duration, duration)
            if transcript:
                is_final = bool(payload.get("is_final"))
                speech_final = bool(payload.get("speech_final"))
                if is_final:
                    final_parts.append(transcript)
                    final_words.extend(words)
                client_ws.send(json.dumps({
                    "type": "transcript",
                    "transcript": transcript,
                    "is_final": is_final,
                    "speech_final": speech_final,
                    "confidence": confidence,
                }))

    receiver = threading.Thread(target=receive_deepgram, daemon=True)
    receiver.start()

    try:
        while True:
            item = client_ws.receive()
            if item is None:
                break
            if isinstance(item, str):
                try:
                    payload = json.loads(item)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "stop":
                    try:
                        deepgram_ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass
                    break
                continue
            deepgram_ws.send_binary(item)
    finally:
        time.sleep(0.5)
        closed.set()
        try:
            deepgram_ws.close()
        except Exception:
            pass

    final_text = " ".join(part.strip() for part in final_parts if part.strip()).strip()
    pronunciation = analyze_from_words(final_text, final_words, latest_duration, acoustic={})
    client_ws.send(json.dumps({"type": "final", "transcript": final_text, "pronunciation": pronunciation}))


def _deepgram_stream_url() -> str:
    params = {
        "model": os.getenv("DEEPGRAM_MODEL", "nova-2"),
        "language": os.getenv("DEEPGRAM_LANGUAGE", os.getenv("ASR_LANGUAGE", "en")),
        "smart_format": "true",
        "punctuate": "true",
        "interim_results": "true",
        "no_delay": os.getenv("DEEPGRAM_NO_DELAY", "true"),
        "utterances": "true",
        "vad_events": "true",
        "endpointing": os.getenv("DEEPGRAM_ENDPOINTING_MS", "300"),
    }
    stream_encoding = os.getenv("DEEPGRAM_STREAM_ENCODING")
    stream_sample_rate = os.getenv("DEEPGRAM_STREAM_SAMPLE_RATE")
    if stream_encoding:
        params["encoding"] = stream_encoding
    if stream_sample_rate:
        params["sample_rate"] = stream_sample_rate
    base_url = os.getenv("DEEPGRAM_STREAM_URL", "wss://api.deepgram.com/v1/listen")
    return base_url + "?" + urlencode(params)


def _parse_stream_result(payload: dict) -> tuple[str, list[dict], float, float | None]:
    channel = payload.get("channel") or {}
    alternatives = channel.get("alternatives") or []
    alternative = alternatives[0] if alternatives else {}
    transcript = alternative.get("transcript") or ""
    words = alternative.get("words") or []
    duration = 0.0
    if words:
        duration = float(words[-1].get("end") or 0.0)
    confidence = alternative.get("confidence")
    confidence = float(confidence) if confidence is not None else None
    return transcript, words, duration, confidence
