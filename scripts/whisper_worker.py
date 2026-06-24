import argparse
import json
import sys
import traceback
from pathlib import Path

from transcribe_local_whisper import patch_whisper_audio_loader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="tiny.en")
    parser.add_argument("--language", default="en")
    return parser.parse_args()


def write_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    args = parse_args()

    try:
        import numpy as np
        import torch
        import whisper
        import imageio_ffmpeg
    except ImportError as exc:
        write_json({"ready": False, "error": f"Missing local Whisper dependency: {exc}"})
        return 3

    patch_whisper_audio_loader(whisper=whisper, np=np, ffmpeg_exe=imageio_ffmpeg.get_ffmpeg_exe())

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model(args.model, device=device)
    write_json({"ready": True, "model": args.model, "device": device})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "__quit__":
            break

        try:
            payload = json.loads(line)
            audio_path = Path(payload["audio"]).resolve()
            if not audio_path.exists():
                write_json({"error": f"Audio file not found: {audio_path}"})
                continue

            result = model.transcribe(
                str(audio_path),
                language=payload.get("language") or args.language,
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
            write_json({
                    "text": (result.get("text") or "").strip(),
                    "segments": segments,
                    "duration": float(result.get("duration") or 0.0),
                })
        except Exception as exc:
            write_json({"error": str(exc), "traceback": traceback.format_exc(limit=5)})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
