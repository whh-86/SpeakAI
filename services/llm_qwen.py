import json
import os
import re
import time
from typing import Any

from services.llm_gemini import LLMServiceError, LIVE_FEEDBACK_PROMPT, REPORT_PROMPT, SYSTEM_PROMPT, normalize_level


def generate_chat_reply(user_text: str, conversation_history: list[dict[str, str]], model: str, timeout_seconds: float, system_prompt: str | None = None) -> dict[str, Any]:
    client = _build_client(timeout_seconds)
    prompt_payload = {
        "history": conversation_history[-6:],
        "user_text": user_text,
    }

    response = _call_with_retry(
        lambda: client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=True)},
            ],
            temperature=0.4,
            response_format={"type": "json_object"},
        ),
        error_prefix="Qwen request failed",
    )

    raw_text = response.choices[0].message.content or ""
    parsed = _extract_json(raw_text)
    if not parsed.get("reply"):
        raise LLMServiceError("Qwen response did not include a reply.", code="invalid_llm_response")

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
        lambda: client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": LIVE_FEEDBACK_PROMPT},
                {"role": "user", "content": conversation_text},
            ],
            temperature=0.4,
            response_format={"type": "json_object"},
        ),
        error_prefix="Qwen live feedback request failed",
    )
    raw_text = (response.choices[0].message.content or "").strip()
    if not raw_text:
        raise LLMServiceError("Qwen returned empty live feedback.", code="empty_live_feedback")
    return _extract_json(raw_text)


def generate_report_feedback(session_summary: dict[str, Any], model: str, timeout_seconds: float) -> str:
    client = _build_client(timeout_seconds)
    response = _call_with_retry(
        lambda: client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": REPORT_PROMPT},
                {"role": "user", "content": json.dumps(session_summary, ensure_ascii=True)},
            ],
            temperature=0.4,
        ),
        error_prefix="Qwen report request failed",
    )

    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise LLMServiceError("Qwen returned empty report feedback.", code="empty_report_feedback")
    return text


def _build_client(timeout_seconds: float):
    api_key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise LLMServiceError("QWEN_API_KEY or DASHSCOPE_API_KEY is not configured.", code="missing_qwen_key", status_code=503)

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - import guard
        raise LLMServiceError("openai package is not installed.", code="missing_openai_sdk", status_code=500) from exc

    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        timeout=timeout_seconds,
    )


_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.5


def _call_with_retry(fn, error_prefix: str):
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn()
        except Exception as exc:
            code = _extract_http_status(exc)
            if code not in _RETRYABLE_STATUS_CODES:
                raise LLMServiceError(_friendly_error_message(error_prefix, exc), code="qwen_error", status_code=code or 502) from exc
            last_exc = exc
            time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
    raise LLMServiceError(_friendly_error_message(f"{error_prefix} after {_MAX_RETRIES} retries", last_exc), code="qwen_error") from last_exc


def _extract_http_status(exc: Exception) -> int:
    status_code = getattr(exc, "status_code", None)
    if status_code:
        return int(status_code)
    match = re.search(r"\b([45]\d{2})\b", str(exc))
    return int(match.group(1)) if match else 0


def _friendly_error_message(prefix: str, exc: Exception | None) -> str:
    if not exc:
        return prefix
    code = _extract_http_status(exc)
    if code in {401, 403}:
        return f"{prefix}: Qwen API key or project permission was rejected. Please check QWEN_API_KEY and QWEN_BASE_URL."
    if code == 429:
        return f"{prefix}: Qwen quota or rate limit was exceeded. Please retry later or check your DashScope billing/quota."
    return f"{prefix}: {exc}"


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
        raise LLMServiceError("Qwen response was not valid JSON.", code="invalid_llm_json")
