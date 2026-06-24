import os
from typing import Any

from services.llm_gemini import LLMServiceError


def generate_chat_reply(user_text: str, conversation_history: list[dict[str, str]], timeout_seconds: float, system_prompt: str | None = None) -> dict[str, Any]:
    provider = current_provider()
    if provider == "qwen":
        from services.llm_qwen import generate_chat_reply as qwen_chat

        return qwen_chat(
            user_text=user_text,
            conversation_history=conversation_history,
            model=os.getenv("QWEN_MODEL", "qwen-plus"),
            timeout_seconds=timeout_seconds,
            system_prompt=system_prompt,
        )

    from services.llm_gemini import generate_chat_reply as gemini_chat

    return gemini_chat(
        user_text=user_text,
        conversation_history=conversation_history,
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        timeout_seconds=timeout_seconds,
        system_prompt=system_prompt,
    )


def generate_live_feedback(messages: list[dict[str, str]], timeout_seconds: float) -> dict[str, Any]:
    provider = current_provider()
    if provider == "qwen":
        from services.llm_qwen import generate_live_feedback as qwen_live

        return qwen_live(
            messages=messages,
            model=os.getenv("QWEN_MODEL", "qwen-plus"),
            timeout_seconds=timeout_seconds,
        )

    from services.llm_gemini import generate_live_feedback as gemini_live

    return gemini_live(
        messages=messages,
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        timeout_seconds=timeout_seconds,
    )


def generate_report_feedback(session_summary: dict[str, Any], timeout_seconds: float) -> str:
    provider = current_provider()
    if provider == "qwen":
        from services.llm_qwen import generate_report_feedback as qwen_report

        return qwen_report(
            session_summary=session_summary,
            model=os.getenv("QWEN_MODEL", "qwen-plus"),
            timeout_seconds=timeout_seconds,
        )

    from services.llm_gemini import generate_report_feedback as gemini_report

    return gemini_report(
        session_summary=session_summary,
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        timeout_seconds=timeout_seconds,
    )


def current_provider() -> str:
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    if provider in {"qwen", "dashscope"}:
        return "qwen"
    return "gemini"
