import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any


SYSTEM_PROMPT = """
You are SpeakAI, an English conversation coach.
Return strict JSON only, with no markdown fences.

Schema:
{
  "reply": "short natural reply in English",
  "level": "A|B|C|D",
  "corrections": [
    {
      "type": "tense|preposition|article|other",
      "original": "the full sentence the user said that contains the error",
      "corrected": "the full corrected version of that sentence",
      "reason": "brief explanation of what was wrong and why"
    }
  ]
}

Rules:
- Reply conversationally to keep the dialogue going.
- Rate the learner with exactly one level: A, B, C, or D.
- A means fluent, accurate, and natural. B means good communication with minor recurring errors. C means understandable but noticeably limited or error-prone. D means basic, fragmented, or difficult to understand.
- If the user's grammar is already good, corrections can be [].
- In corrections, always write the FULL sentence (not just the fragment) so the learner sees the complete correct form.
- Keep reply under 90 words.
- IMPORTANT: This is a spoken English app. The input is transcribed from speech, so ignore ALL punctuation errors (commas, periods, apostrophes, etc.). Only correct real spoken grammar mistakes such as wrong tense, missing or wrong articles, wrong preposition choice, subject-verb disagreement, or unnatural word order.
""".strip()


REPORT_PROMPT = """
You are an English speaking coach.
Given a session summary that includes grammar error breakdown and pronunciation metrics, return a short paragraph of personalized feedback in plain text.
Address both dimensions: mention the learner's most frequent grammar error type, comment on their pronunciation score and speaking rate if the data is available, and give one concrete next-step practice suggestion.
Keep the answer under 120 words.
""".strip()


LIVE_FEEDBACK_PROMPT = """
You are an expert English speaking coach. Analyze the learner's messages from a live conversation session and return ONLY a valid JSON object with no markdown fences:
{
  "level": "B1",
  "overall": "One to two sentence encouraging but honest assessment of the learner's current level and performance.",
  "strengths": ["specific strength observed in their messages", "another specific strength"],
  "improvements": ["specific grammar or vocabulary area to work on", "another specific area"],
  "tip": "One concrete, actionable practice exercise the learner can do today."
}
Rules:
- level must be one of: A1, A2, B1, B2, C1, C2
- strengths and improvements must each have exactly 2 items, grounded in what you actually observed
- tip should be specific (e.g. "Write 5 sentences using 'despite' and 'although' to practice contrasting ideas")
- Return only the JSON object, nothing else
""".strip()


@dataclass
class LLMServiceError(Exception):
    message: str
    code: str = "llm_error"
    status_code: int = 502

    def __str__(self) -> str:
        return self.message


def generate_chat_reply(user_text: str, conversation_history: list[dict[str, str]], model: str, timeout_seconds: float, system_prompt: str | None = None) -> dict[str, Any]:
    client = _build_client(timeout_seconds)
    prompt_payload = {
        "history": conversation_history[-6:],
        "user_text": user_text,
    }

    response = _call_with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents=json.dumps(prompt_payload, ensure_ascii=True),
            config=_build_config(system_prompt or SYSTEM_PROMPT, "application/json"),
        ),
        error_prefix="Gemini request failed",
    )

    raw_text = getattr(response, "text", None) or ""
    parsed = _extract_json(raw_text)
    if not parsed.get("reply"):
        raise LLMServiceError("Gemini response did not include a reply.", code="invalid_llm_response")

    parsed["corrections"] = parsed.get("corrections") or []
    parsed["level"] = normalize_level(parsed.get("level"))
    return parsed


def generate_live_feedback(messages: list[dict[str, str]], model: str, timeout_seconds: float) -> dict[str, Any]:
    client = _build_client(timeout_seconds)
    conversation_text = "\n".join(
        f"{m['role'].upper()}: {m.get('text') or m.get('content', '')}"
        for m in messages
        if (m.get('text') or m.get('content', '')).strip()
    )
    response = _call_with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents=conversation_text,
            config=_build_config(LIVE_FEEDBACK_PROMPT, "application/json"),
        ),
        error_prefix="Gemini live feedback request failed",
    )
    raw_text = (getattr(response, "text", None) or "").strip()
    if not raw_text:
        raise LLMServiceError("Gemini returned empty live feedback.", code="empty_live_feedback")
    return _extract_json(raw_text)


def generate_report_feedback(session_summary: dict[str, Any], model: str, timeout_seconds: float) -> str:
    client = _build_client(timeout_seconds)
    response = _call_with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents=json.dumps(session_summary, ensure_ascii=True),
            config=_build_config(REPORT_PROMPT, "text/plain"),
        ),
        error_prefix="Gemini report request failed",
    )

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise LLMServiceError("Gemini returned empty report feedback.", code="empty_report_feedback")
    return text


def _build_client(timeout_seconds: float):
    developer_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true"

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - import guard
        raise LLMServiceError("google-genai package is not installed.", code="missing_gemini_sdk", status_code=500) from exc

    api_version = "v1alpha"
    client_kwargs: dict[str, Any]
    if use_vertex:
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION")
        if not project or not location:
            raise LLMServiceError(
                "Vertex mode requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION.",
                code="missing_vertex_config",
                status_code=503,
            )
        api_version = "v1"
        client_kwargs = {
            "vertexai": True,
            "project": project,
            "location": location,
            "http_options": types.HttpOptions(api_version=api_version),
        }
    else:
        if not developer_api_key:
            raise LLMServiceError("GEMINI_API_KEY or GOOGLE_API_KEY is not configured.", code="missing_gemini_key", status_code=503)
        client_kwargs = {
            "api_key": developer_api_key,
            "http_options": types.HttpOptions(api_version=api_version),
        }

    try:
        client = genai.Client(**client_kwargs)
    except Exception as exc:  # pragma: no cover - client creation path
        raise LLMServiceError(f"Failed to initialize Gemini client: {exc}", code="gemini_client_init") from exc

    return client


def _build_config(system_instruction: str, response_mime_type: str):
    from google.genai import types

    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.4,
        response_mime_type=response_mime_type,
    )


def _extract_json(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        brace_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace_match:
            return json.loads(brace_match.group(0))
        raise LLMServiceError("Gemini response was not valid JSON.", code="invalid_llm_json")


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.5  # seconds; doubles each attempt


def _call_with_retry(fn, error_prefix: str):
    """Call fn(), retrying up to _MAX_RETRIES times on transient Gemini errors (429/5xx)."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn()
        except Exception as exc:
            raw = str(exc)
            code = _extract_http_status(raw)
            if code == 429 and _is_quota_exhausted(raw):
                retry_after = _extract_retry_delay(raw)
                suffix = f" Try again in about {retry_after} seconds." if retry_after else " Try again later."
                raise LLMServiceError(
                    "Gemini quota exceeded for the current model/API key." + suffix,
                    code="gemini_quota_exceeded",
                    status_code=429,
                ) from exc
            if code not in _RETRYABLE_STATUS_CODES:
                raise LLMServiceError(f"{error_prefix}: {exc}") from exc
            last_exc = exc
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            time.sleep(delay)
    raise LLMServiceError(f"{error_prefix} after {_MAX_RETRIES} retries: {last_exc}") from last_exc


def _extract_http_status(error_text: str) -> int:
    """Pull the HTTP status code out of a Gemini SDK error string, e.g. '503 UNAVAILABLE'."""
    match = re.search(r"\b([45]\d{2})\b", error_text)
    return int(match.group(1)) if match else 0


def _is_quota_exhausted(error_text: str) -> bool:
    lowered = error_text.lower()
    return "resource_exhausted" in lowered or "quota exceeded" in lowered


def _extract_retry_delay(error_text: str) -> int | None:
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)s", error_text)
    if match:
        return int(match.group(1))
    match = re.search(r"retry in ([\d.]+)s", error_text, re.IGNORECASE)
    if match:
        return round(float(match.group(1)))
    return None


def normalize_level(level: Any) -> str:
    value = str(level or "").strip().upper()
    aliases = {
        "A2": "D",
        "B1": "C",
        "B2": "B",
        "C1": "A",
        "C2": "A",
    }
    value = aliases.get(value, value)
    if value in {"A", "B", "C", "D"}:
        return value
    return "B"
