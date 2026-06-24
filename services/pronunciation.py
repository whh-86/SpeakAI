import re
from typing import Any

_FILLER_RE = re.compile(
    r"\b(um+|uh+|er+|ah+|like|you know|sort of|kind of|basically|literally)\b",
    re.IGNORECASE,
)


def analyze_from_whisper(text: str, segments: list[dict], duration: float, acoustic: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full pronunciation analysis using Whisper segment timestamps and confidence scores."""
    word_count = len(text.split()) if text.strip() else 0
    fillers = _FILLER_RE.findall(text)
    filler_count = len(fillers)

    speaking_rate_wpm = round(word_count / (duration / 60), 1) if duration and duration > 0 else 0.0

    # Clarity from Whisper avg_logprob: 0=perfect, -0.5=ok, -1.0=very unclear → map to 0-100
    clarity_score = 100
    if segments:
        avg_lp = sum(s.get("avg_logprob", 0.0) for s in segments) / len(segments)
        clarity_score = max(0, min(100, int((avg_lp + 1.0) * 100)))

    pauses = []
    for i in range(1, len(segments)):
        gap = segments[i].get("start", 0) - segments[i - 1].get("end", 0)
        if gap > 0.5:
            pauses.append(round(gap, 2))

    rate_score = _rate_score(speaking_rate_wpm)
    filler_penalty = max(0, 100 - filler_count * 10)
    acoustic = acoustic or {}
    volume_stability = acoustic.get("volume_stability")
    volume_score = volume_stability if volume_stability is not None else 70
    composite = int(clarity_score * 0.4 + rate_score * 0.25 + filler_penalty * 0.2 + volume_score * 0.15)

    return {
        "score": composite,
        "label": _score_label(composite),
        "speaking_rate_wpm": speaking_rate_wpm,
        "rate_score": rate_score,
        "clarity_score": clarity_score,
        "filler_count": filler_count,
        "filler_score": filler_penalty,
        "filler_words": sorted(set(f.lower() for f in fillers)),
        "pause_count": len(pauses),
        "pause_frequency_per_min": round(len(pauses) / (duration / 60), 1) if duration and duration > 0 else 0.0,
        "volume_stability": volume_stability,
        "quiet_ratio": acoustic.get("quiet_ratio"),
        "duration_seconds": round(duration, 1) if duration else 0.0,
    }


def analyze_from_words(text: str, words: list[dict], duration: float, acoustic: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pronunciation-style analysis from word timestamps/confidence, e.g. Deepgram words."""
    fillers = _FILLER_RE.findall(text)
    filler_count = len(fillers)
    word_count = len(words) if words else (len(text.split()) if text.strip() else 0)
    speaking_rate_wpm = round(word_count / (duration / 60), 1) if duration and duration > 0 else None

    confidence_values = [float(item["confidence"]) for item in words if item.get("confidence") is not None]
    clarity_score = round(sum(confidence_values) / len(confidence_values) * 100) if confidence_values else None

    pauses = []
    ordered_words = [item for item in words if item.get("start") is not None and item.get("end") is not None]
    for index in range(1, len(ordered_words)):
        gap = float(ordered_words[index]["start"]) - float(ordered_words[index - 1]["end"])
        if gap > 0.5:
            pauses.append(round(gap, 2))

    rate_score = _rate_score(speaking_rate_wpm) if speaking_rate_wpm is not None else None
    filler_penalty = max(0, 100 - filler_count * 10)
    acoustic = acoustic or {}
    volume_stability = acoustic.get("volume_stability")

    weighted_parts = []
    if clarity_score is not None:
        weighted_parts.append((clarity_score, 0.4))
    if rate_score is not None:
        weighted_parts.append((rate_score, 0.25))
    weighted_parts.append((filler_penalty, 0.2))
    if volume_stability is not None:
        weighted_parts.append((volume_stability, 0.15))
    total_weight = sum(weight for _, weight in weighted_parts)
    composite = round(sum(value * weight for value, weight in weighted_parts) / total_weight) if total_weight else filler_penalty

    return {
        "score": composite,
        "label": _score_label(composite),
        "speaking_rate_wpm": speaking_rate_wpm,
        "rate_score": rate_score,
        "clarity_score": clarity_score,
        "filler_count": filler_count,
        "filler_score": filler_penalty,
        "filler_words": sorted(set(f.lower() for f in fillers)),
        "pause_count": len(pauses),
        "pause_frequency_per_min": round(len(pauses) / (duration / 60), 1) if duration and duration > 0 else None,
        "volume_stability": volume_stability,
        "quiet_ratio": acoustic.get("quiet_ratio"),
        "duration_seconds": round(duration, 1) if duration else acoustic.get("duration_seconds"),
    }


def analyze_basic(text: str, acoustic: dict[str, Any] | None = None) -> dict[str, Any]:
    """Filler-word-only analysis used when Whisper segment data is unavailable (e.g. OpenAI API path)."""
    fillers = _FILLER_RE.findall(text)
    filler_count = len(fillers)
    filler_penalty = max(0, 100 - filler_count * 10)
    acoustic = acoustic or {}
    duration = acoustic.get("duration_seconds")
    word_count = len(text.split()) if text.strip() else 0
    speaking_rate_wpm = round(word_count / (duration / 60), 1) if duration and duration > 0 else None
    rate_score = _rate_score(speaking_rate_wpm) if speaking_rate_wpm is not None else None
    volume_stability = acoustic.get("volume_stability")
    score_parts = [filler_penalty]
    if rate_score is not None:
        score_parts.append(rate_score)
    if volume_stability is not None:
        score_parts.append(volume_stability)
    score = round(sum(score_parts) / len(score_parts))
    return {
        "score": score,
        "label": _score_label(score),
        "speaking_rate_wpm": speaking_rate_wpm,
        "rate_score": rate_score,
        "clarity_score": None,
        "filler_count": filler_count,
        "filler_score": filler_penalty,
        "filler_words": sorted(set(f.lower() for f in fillers)),
        "pause_count": None,
        "pause_frequency_per_min": None,
        "volume_stability": volume_stability,
        "quiet_ratio": acoustic.get("quiet_ratio"),
        "duration_seconds": duration,
    }


def aggregate(scores: list[dict]) -> dict[str, Any]:
    """Aggregate per-turn pronunciation metrics into a session-level summary."""
    if not scores:
        return {
            "avg_score": 0,
            "label": "N/A",
            "avg_speaking_rate_wpm": None,
            "total_filler_count": 0,
            "avg_pause_count": None,
            "avg_pause_frequency_per_min": None,
            "avg_volume_stability": None,
            "avg_quiet_ratio": None,
        }

    valid_scores = [s["score"] for s in scores if s.get("score") is not None]
    avg_score = round(sum(valid_scores) / len(valid_scores)) if valid_scores else 0

    wpm_values = [s["speaking_rate_wpm"] for s in scores if s.get("speaking_rate_wpm") is not None]
    avg_wpm = round(sum(wpm_values) / len(wpm_values), 1) if wpm_values else None

    total_fillers = sum(s.get("filler_count", 0) for s in scores)

    pause_values = [s["pause_count"] for s in scores if s.get("pause_count") is not None]
    avg_pauses = round(sum(pause_values) / len(pause_values), 1) if pause_values else None
    pause_frequency_values = [s["pause_frequency_per_min"] for s in scores if s.get("pause_frequency_per_min") is not None]
    avg_pause_frequency = round(sum(pause_frequency_values) / len(pause_frequency_values), 1) if pause_frequency_values else None
    volume_values = [s["volume_stability"] for s in scores if s.get("volume_stability") is not None]
    avg_volume_stability = round(sum(volume_values) / len(volume_values)) if volume_values else None
    quiet_values = [s["quiet_ratio"] for s in scores if s.get("quiet_ratio") is not None]
    avg_quiet_ratio = round(sum(quiet_values) / len(quiet_values), 2) if quiet_values else None

    return {
        "avg_score": avg_score,
        "label": _score_label(avg_score),
        "avg_speaking_rate_wpm": avg_wpm,
        "total_filler_count": total_fillers,
        "avg_pause_count": avg_pauses,
        "avg_pause_frequency_per_min": avg_pause_frequency,
        "avg_volume_stability": avg_volume_stability,
        "avg_quiet_ratio": avg_quiet_ratio,
    }


def _rate_score(wpm: float | None) -> int:
    """Score speaking rate: native target 100-150 WPM scores 100, outside penalised."""
    if not wpm:
        return 50
    if 100 <= wpm <= 150:
        return 100
    if wpm < 100:
        return max(40, int(wpm / 100 * 100))
    return max(40, int((200 - wpm) / 50 * 100))


def _score_label(score: int) -> str:
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 55:
        return "Fair"
    return "Needs Work"
