import os
import subprocess
import sys
import threading
from json import dumps
from json import JSONDecodeError, loads
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from services.acoustics import analyze_audio
from services.pronunciation import analyze_basic, analyze_from_whisper, analyze_from_words


@dataclass
class ASRServiceError(Exception):
    message: str
    code: str = "asr_error"
    status_code: int = 502

    def __str__(self) -> str:
        return self.message


_WORKER = None
_WORKER_LOCK = threading.Lock()


def transcribe_audio(audio_path: Path, model: str, timeout_seconds: float) -> dict[str, Any]:
    """Return {"text": str, "pronunciation": dict} for every backend."""
    requested_backend, requested_model = _split_asr_model(model)
    backend = requested_backend or os.getenv("ASR_BACKEND", "local_whisper").strip().lower()
    if backend in {"local_whisper", "whisper_local", "whisper"}:
        return _transcribe_local_whisper(audio_path=audio_path, model=requested_model, timeout_seconds=timeout_seconds)
    if backend in {"deepgram", "deepgram_api"}:
        return _transcribe_deepgram_api(audio_path=audio_path, model=requested_model, timeout_seconds=timeout_seconds)
    if backend in {"openai", "openai_api"}:
        return _transcribe_openai_api(audio_path=audio_path, model=requested_model, timeout_seconds=timeout_seconds)
    raise ASRServiceError(f"Unsupported ASR_BACKEND '{backend}'.", code="unsupported_asr_backend", status_code=500)


def preload_local_whisper(model: str, timeout_seconds: float) -> None:
    _get_local_whisper_worker(model=model, timeout_seconds=timeout_seconds)


def _transcribe_openai_api(audio_path: Path, model: str, timeout_seconds: float) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ASRServiceError("OPENAI_API_KEY is not configured.", code="missing_openai_key", status_code=503)

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - import guard
        raise ASRServiceError("openai package is not installed.", code="missing_openai_sdk", status_code=500) from exc

    client = OpenAI(api_key=api_key, timeout=timeout_seconds)

    try:
        with audio_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(model=model, file=audio_file)
    except Exception as exc:  # pragma: no cover - network path
        raise ASRServiceError(f"OpenAI transcription request failed: {exc}") from exc

    text = getattr(response, "text", None) or ""
    if not text.strip():
        raise ASRServiceError("OpenAI transcription returned empty text.", code="empty_transcription", status_code=502)
    text = text.strip()
    acoustic = analyze_audio(audio_path, timeout_seconds=timeout_seconds)
    return {"text": text, "pronunciation": analyze_basic(text, acoustic=acoustic)}


def _split_asr_model(model: str) -> tuple[str | None, str]:
    value = (model or "").strip()
    if ":" not in value:
        return None, value
    backend, actual_model = value.split(":", 1)
    return backend.strip().lower(), actual_model.strip()


def _transcribe_deepgram_api(audio_path: Path, model: str, timeout_seconds: float) -> dict[str, Any]:
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise ASRServiceError("DEEPGRAM_API_KEY is not configured.", code="missing_deepgram_key", status_code=503)

    params = {
        "model": model or os.getenv("DEEPGRAM_MODEL", "nova-2"),
        "language": os.getenv("DEEPGRAM_LANGUAGE", os.getenv("ASR_LANGUAGE", "en")),
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
    }
    url = os.getenv("DEEPGRAM_LISTEN_URL", "https://api.deepgram.com/v1/listen") + "?" + urlencode(params)
    request = Request(
        url,
        data=audio_path.read_bytes(),
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": _guess_content_type(audio_path),
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ASRServiceError(_friendly_deepgram_error(exc.code, body), code="deepgram_error", status_code=exc.code) from exc
    except (URLError, TimeoutError) as exc:
        raise ASRServiceError(f"Deepgram transcription request failed: {exc}", code="deepgram_request_failed", status_code=502) from exc
    except JSONDecodeError as exc:
        raise ASRServiceError("Deepgram returned invalid JSON.", code="invalid_deepgram_response", status_code=502) from exc

    text, words, duration, confidence = _parse_deepgram_result(payload)
    if not text.strip():
        raise ASRServiceError("Deepgram transcription returned empty text.", code="empty_transcription", status_code=502)

    acoustic = analyze_audio(audio_path, timeout_seconds=timeout_seconds)
    if not duration:
        duration = acoustic.get("duration_seconds") or 0.0
    pronunciation = analyze_from_words(text.strip(), words, duration, acoustic=acoustic)
    if confidence is not None and pronunciation.get("clarity_score") is None:
        pronunciation["clarity_score"] = round(confidence * 100)
    return {"text": text.strip(), "pronunciation": pronunciation}


def _transcribe_local_whisper(audio_path: Path, model: str, timeout_seconds: float) -> dict[str, Any]:
    if os.getenv("WHISPER_USE_WORKER", "true").lower() == "true":
        try:
            return _transcribe_local_whisper_worker(audio_path=audio_path, model=model, timeout_seconds=timeout_seconds)
        except ASRServiceError:
            if os.getenv("WHISPER_WORKER_FALLBACK", "true").lower() != "true":
                raise

    command = _build_local_whisper_command(audio_path=audio_path, model=model)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ASRServiceError("Local Whisper transcription timed out.", code="local_whisper_timeout", status_code=504) from exc
    except OSError as exc:
        raise ASRServiceError(f"Failed to start local Whisper process: {exc}", code="local_whisper_launch_failed", status_code=500) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise ASRServiceError(
            f"Local Whisper transcription failed with exit code {result.returncode}: {stderr or 'no stderr output'}",
            code="local_whisper_failed",
            status_code=502,
        )

    asr_result = _parse_local_whisper_result(result.stdout, audio_path=audio_path)
    if not asr_result.get("text"):
        raise ASRServiceError("Local Whisper returned empty text.", code="empty_transcription", status_code=502)
    return asr_result


def _transcribe_local_whisper_worker(audio_path: Path, model: str, timeout_seconds: float) -> str:
    worker = _get_local_whisper_worker(model=model, timeout_seconds=timeout_seconds)
    return worker.transcribe(audio_path=audio_path, timeout_seconds=timeout_seconds)


def _get_local_whisper_worker(model: str, timeout_seconds: float):
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive or _WORKER.model != model:
            if _WORKER is not None:
                _WORKER.stop()
            _WORKER = LocalWhisperWorker(model=model)
            _WORKER.start(timeout_seconds=timeout_seconds)
        return _WORKER


class LocalWhisperWorker:
    def __init__(self, model: str) -> None:
        self.model = model
        self.process = None
        self.lock = threading.Lock()
        self.is_alive = False

    def start(self, timeout_seconds: float) -> None:
        command = _build_local_whisper_worker_command(model=self.model)
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=Path(__file__).resolve().parents[1],
                bufsize=1,
            )
        except OSError as exc:
            raise ASRServiceError(f"Failed to start local Whisper worker: {exc}", code="local_whisper_launch_failed", status_code=500) from exc

        ready = self._read_response(timeout_seconds=timeout_seconds)
        if ready.get("ready") is not True:
            self.stop()
            raise ASRServiceError(
                f"Local Whisper worker failed to initialize: {ready.get('error') or ready}",
                code="local_whisper_worker_failed",
                status_code=502,
            )
        self.is_alive = True

    def transcribe(self, audio_path: Path, timeout_seconds: float) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise ASRServiceError("Local Whisper worker is not running.", code="local_whisper_worker_stopped", status_code=502)

        with self.lock:
            try:
                self.process.stdin.write(dumps_json({"audio": str(audio_path), "language": os.getenv("ASR_LANGUAGE", "en")}) + "\n")
                self.process.stdin.flush()
                response = self._read_response(timeout_seconds=timeout_seconds)
            except (BrokenPipeError, OSError) as exc:
                self.is_alive = False
                raise ASRServiceError(f"Local Whisper worker stopped unexpectedly: {exc}", code="local_whisper_worker_stopped", status_code=502) from exc

        if response.get("error"):
            raise ASRServiceError(
                f"Local Whisper worker transcription failed: {response['error']}",
                code="local_whisper_failed",
                status_code=502,
            )

        text = (response.get("text") or "").strip()
        if not text:
            raise ASRServiceError("Local Whisper returned empty text.", code="empty_transcription", status_code=502)

        segments = response.get("segments") or []
        duration = float(response.get("duration") or 0.0)
        acoustic = analyze_audio(audio_path, timeout_seconds=timeout_seconds)
        pronunciation = (
            analyze_from_whisper(text, segments, duration, acoustic=acoustic) if segments else analyze_basic(text, acoustic=acoustic)
        )
        return {"text": text, "pronunciation": pronunciation}

    def _read_response(self, timeout_seconds: float) -> dict:
        if self.process is None or self.process.stdout is None:
            raise ASRServiceError("Local Whisper worker is not running.", code="local_whisper_worker_stopped", status_code=502)

        result = {}

        def read_line() -> None:
            line = self.process.stdout.readline()
            if line:
                result["line"] = line

        reader = threading.Thread(target=read_line, daemon=True)
        reader.start()
        reader.join(timeout_seconds)
        if reader.is_alive():
            self.stop()
            raise ASRServiceError("Local Whisper worker timed out.", code="local_whisper_timeout", status_code=504)

        line = result.get("line", "").strip()
        if not line:
            self.is_alive = False
            raise ASRServiceError("Local Whisper worker exited without output.", code="local_whisper_worker_stopped", status_code=502)

        try:
            return loads(line)
        except JSONDecodeError as exc:
            raise ASRServiceError(f"Local Whisper worker returned invalid JSON: {line}", code="local_whisper_worker_failed", status_code=502) from exc

    def stop(self) -> None:
        process = self.process
        self.is_alive = False
        self.process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.write("__quit__\n")
                process.stdin.flush()
        except OSError:
            pass
        try:
            process.terminate()
        except OSError:
            pass


def _build_local_whisper_command(audio_path: Path, model: str) -> list[str]:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "transcribe_local_whisper.py"
    python_exe = os.getenv("WHISPER_PYTHON_EXE", "").strip()
    language = os.getenv("ASR_LANGUAGE", "en").strip() or "en"

    if python_exe:
        return [python_exe, str(script_path), "--audio", str(audio_path), "--model", model, "--language", language]

    conda_env = os.getenv("WHISPER_CONDA_ENV", "whisper-exp").strip() or "whisper-exp"
    return ["conda", "run", "-n", conda_env, "python", str(script_path), "--audio", str(audio_path), "--model", model, "--language", language]


def _build_local_whisper_worker_command(model: str) -> list[str]:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "whisper_worker.py"
    python_exe = os.getenv("WHISPER_PYTHON_EXE", "").strip()
    language = os.getenv("ASR_LANGUAGE", "en").strip() or "en"

    if python_exe:
        return [python_exe, "-u", str(script_path), "--model", model, "--language", language]

    conda_env = os.getenv("WHISPER_CONDA_ENV", "whisper-exp").strip() or "whisper-exp"
    return ["conda", "run", "--no-capture-output", "-n", conda_env, "python", "-u", str(script_path), "--model", model, "--language", language]


def dumps_json(payload: dict) -> str:
    return dumps(payload, ensure_ascii=False)


def _parse_local_whisper_result(stdout: str, audio_path: Path | None = None) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            payload = loads(line)
            text = (payload.get("text") or "").strip()
            if text:
                segments = payload.get("segments") or []
                duration = float(payload.get("duration") or 0.0)
                acoustic = analyze_audio(audio_path, timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "45"))) if audio_path else {}
                pronunciation = (
                    analyze_from_whisper(text, segments, duration, acoustic=acoustic) if segments else analyze_basic(text, acoustic=acoustic)
                )
                return {"text": text, "pronunciation": pronunciation}
        except JSONDecodeError:
            continue
    return {"text": "", "pronunciation": analyze_basic("")}


def _parse_deepgram_result(payload: dict[str, Any]) -> tuple[str, list[dict], float, float | None]:
    channels = payload.get("results", {}).get("channels") or []
    alternatives = channels[0].get("alternatives") if channels else []
    alternative = alternatives[0] if alternatives else {}
    text = alternative.get("transcript") or ""
    words = alternative.get("words") or []
    duration = float(payload.get("metadata", {}).get("duration") or 0.0)
    if not duration and words:
        duration = float(words[-1].get("end") or 0.0)
    confidence = alternative.get("confidence")
    confidence = float(confidence) if confidence is not None else None
    return text, words, duration, confidence


def _guess_content_type(audio_path: Path) -> str:
    suffix = audio_path.suffix.lower()
    if suffix == ".webm":
        return "audio/webm"
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix in {".m4a", ".mp4"}:
        return "audio/mp4"
    return "application/octet-stream"


def _friendly_deepgram_error(status_code: int, body: str) -> str:
    if status_code in {401, 403}:
        return "Deepgram API key was rejected. Please check DEEPGRAM_API_KEY."
    if status_code == 429:
        return "Deepgram quota or rate limit was exceeded. Please retry later or check your Deepgram plan."
    return f"Deepgram transcription failed with status {status_code}: {body[:300]}"
