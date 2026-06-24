import argparse
import json
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", default="base.en")
    parser.add_argument("--language", default="en")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audio_path = Path(args.audio).resolve()
    if not audio_path.exists():
        print(json.dumps({"error": f"Audio file not found: {audio_path}"}), file=sys.stderr)
        return 2

    try:
        import numpy as np
        import torch
        import whisper
        import imageio_ffmpeg
    except ImportError as exc:
        print(json.dumps({"error": f"Missing local Whisper dependency: {exc}"}), file=sys.stderr)
        return 3

    patch_whisper_audio_loader(whisper=whisper, np=np, ffmpeg_exe=imageio_ffmpeg.get_ffmpeg_exe())

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model(args.model, device=device)
    result = model.transcribe(
        str(audio_path),
        language=args.language,
        fp16=(device == "cuda"),
        verbose=False,
        task="transcribe",
    )
    segments = [
        {
            "start": float(s.get("start", 0)),
            "end": float(s.get("end", 0)),
            "avg_logprob": float(s.get("avg_logprob", 0.0)),
        }
        for s in result.get("segments", [])
    ]
    print(json.dumps({
        "text": (result.get("text") or "").strip(),
        "segments": segments,
        "duration": float(result.get("duration") or 0.0),
    }, ensure_ascii=False))
    return 0


def patch_whisper_audio_loader(whisper, np, ffmpeg_exe: str) -> None:
    ffmpeg_dir = str(Path(ffmpeg_exe).resolve().parent)
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    def load_audio(file_path: str, sr: int = whisper.audio.SAMPLE_RATE):
        command = [
            ffmpeg_exe,
            "-nostdin",
            "-threads",
            "0",
            "-i",
            file_path,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(sr),
            "-",
        ]
        try:
            output = whisper.audio.run(command, capture_output=True, check=True).stdout
        except Exception as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            raise RuntimeError(f"Failed to load audio with bundled ffmpeg: {stderr.decode(errors='ignore')}") from exc
        return np.frombuffer(output, np.int16).flatten().astype(np.float32) / 32768.0

    whisper.audio.load_audio = load_audio


if __name__ == "__main__":
    raise SystemExit(main())
