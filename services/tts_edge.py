import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TTSServiceError(Exception):
    message: str
    code: str = "tts_error"
    status_code: int = 502

    def __str__(self) -> str:
        return self.message


def synthesize_to_file(text: str, output_path: Path, voice: str, timeout_seconds: float) -> None:
    if not text.strip():
        raise TTSServiceError("TTS text cannot be empty.", code="missing_tts_text", status_code=400)

    try:
        asyncio.run(_synthesize(text=text, output_path=output_path, voice=voice, timeout_seconds=timeout_seconds))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_synthesize(text=text, output_path=output_path, voice=voice, timeout_seconds=timeout_seconds))
        finally:
            loop.close()
    except Exception as exc:  # pragma: no cover - network path
        raise TTSServiceError(f"edge-tts synthesis failed: {exc}") from exc


async def _synthesize(text: str, output_path: Path, voice: str, timeout_seconds: float) -> None:
    try:
        import edge_tts
    except ImportError as exc:  # pragma: no cover - import guard
        raise TTSServiceError("edge-tts package is not installed.", code="missing_edge_tts_sdk", status_code=500) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text=text, voice=voice)
    try:
        await asyncio.wait_for(communicate.save(str(output_path)), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise TTSServiceError("edge-tts synthesis timed out.", code="tts_timeout", status_code=504) from exc
