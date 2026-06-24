import os
import struct
import subprocess
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


def analyze_audio(audio_path: Path, timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Extract cheap acoustic metrics from an audio file using ffmpeg when available."""
    pcm = _decode_pcm(audio_path, timeout_seconds=timeout_seconds)
    if not pcm:
        return _empty_metrics()

    sample_rate = 16000
    sample_width = 2
    frame_ms = 100
    frame_size = int(sample_rate * frame_ms / 1000) * sample_width
    rms_values = []
    for start in range(0, len(pcm), frame_size):
        frame = pcm[start : start + frame_size]
        if len(frame) < frame_size:
            continue
        rms_values.append(_rms_16bit(frame))

    if not rms_values:
        return _empty_metrics()

    non_silent = [value for value in rms_values if value > 120]
    active_values = non_silent or rms_values
    avg_rms = mean(active_values)
    rms_std = pstdev(active_values) if len(active_values) > 1 else 0.0
    coefficient = rms_std / avg_rms if avg_rms else 1.0
    stability = max(0, min(100, round(100 - coefficient * 100)))

    quiet_ratio = round(1 - (len(non_silent) / len(rms_values)), 2)
    duration = round(len(pcm) / (sample_rate * sample_width), 2)
    return {
        "volume_stability": stability,
        "avg_rms": round(avg_rms, 1),
        "quiet_ratio": quiet_ratio,
        "duration_seconds": duration,
    }


def _decode_pcm(audio_path: Path, timeout_seconds: float) -> bytes:
    ffmpeg = os.getenv("FFMPEG_EXE", "ffmpeg")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=min(timeout_seconds, 10.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return b""
    if result.returncode != 0:
        return b""
    return result.stdout


def _empty_metrics() -> dict[str, Any]:
    return {
        "volume_stability": None,
        "avg_rms": None,
        "quiet_ratio": None,
        "duration_seconds": None,
    }


def _rms_16bit(frame: bytes) -> float:
    sample_count = len(frame) // 2
    if not sample_count:
        return 0.0
    total = 0
    for (sample,) in struct.iter_unpack("<h", frame[: sample_count * 2]):
        total += sample * sample
    return (total / sample_count) ** 0.5
