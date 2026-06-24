# SpeakAI Flask Demo

SpeakAI is a local Flask app for spoken English coaching. The browser records audio, `/api/chat` sends it through local Whisper or Deepgram transcription, Gemini generates a reply plus structured corrections, and `edge-tts` synthesizes the answer for playback.

## Project Layout

- `app.py` — Flask app, API routes, session state, error handling
- `templates/index.html` — Flask template (all UI)
- `static/` — CSS, JS, images, generated audio files
- `services/db.py` — SQLite persistence (sessions, messages, corrections, pronunciation scores)
- `services/asr_openai.py` — ASR wrapper: local Whisper or OpenAI transcription
- `services/asr_stream_deepgram.py` — Deepgram streaming ASR via WebSocket
- `services/llm.py` — LLM provider router (Gemini / Qwen)
- `services/llm_gemini.py` — Gemini chat/report wrapper, default system prompt
- `services/llm_qwen.py` — Qwen chat wrapper (optional alternative)
- `services/tts_edge.py` — edge-tts wrapper
- `services/pronunciation.py` — per-turn pronunciation scoring (WPM, pauses, fillers, clarity)
- `services/acoustics.py` — server-side volume analysis via ffmpeg
- `scripts/transcribe_local_whisper.py` — helper script executed inside the local Whisper conda env
- `tests/` — API and frontend smoke tests

## Setup

1. Create a virtual environment.
2. Install main app dependencies.
3. Install local Whisper dependencies in a separate conda environment.
4. Copy `.env.example` to `.env` and fill in your credentials.
5. Start Flask.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

For the local Whisper environment:

```powershell
conda create -n whisper-exp python=3.11 -y
conda activate whisper-exp
pip install -r requirements-whisper.txt
```

Open `http://127.0.0.1:5000`.

## Credentials

Use only one Gemini auth path at a time.

### Recommended: Local Whisper + Gemini Developer API

```env
ASR_BACKEND=local_whisper
ASR_MODEL=base.en
WHISPER_CONDA_ENV=whisper-exp
GEMINI_API_KEY=your_gemini_key
```

If your project Python differs from the Whisper environment, the app calls `conda run -n whisper-exp ...` automatically. You can also point directly to a Python executable:

```env
WHISPER_PYTHON_EXE=D:\anaconda3\envs\whisper-exp\python.exe
```

### Optional: Deepgram streaming ASR + Gemini

```env
ASR_BACKEND=deepgram
DEEPGRAM_API_KEY=your_deepgram_key
GEMINI_API_KEY=your_gemini_key
```

### Optional fallback: OpenAI ASR + Gemini

```env
ASR_BACKEND=openai_api
OPENAI_API_KEY=your_openai_key
GEMINI_API_KEY=your_gemini_key
```

### Vertex AI alternative

```env
ASR_BACKEND=local_whisper
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=C:\path\to\service-account.json
```

`edge-tts` does not require a cloud API key.

## Safe Secret Handling

- Do not paste secrets into source files or commit them to git.
- Put keys only in `.env`, which is already ignored by `.gitignore`.
- For Vertex service-account JSON, place it outside the repo if possible and point `GOOGLE_APPLICATION_CREDENTIALS` at that absolute path.
- Keep `.env.example` as a blank template only. Never store real API keys there.

## API Endpoints

### `GET /api/settings`

Returns server-side defaults for voice, ASR model, system prompt, and greeting.

### `POST /api/chat`

Multipart form upload. Transcribes audio, generates an AI reply, saves the turn.

Form fields:
- `audio` — required audio file (webm / mp4)
- `session_id` — optional; omit to start a new session
- `asr_model` — e.g. `deepgram:nova-2` or `whisper:base.en`
- `voice` — TTS voice name, e.g. `en-US-AriaNeural`
- `prompt` — optional system prompt override

Response:

```json
{
  "session_id": "uuid",
  "transcription": "I goes to school yesterday.",
  "reply": "You can say, I went to school yesterday.",
  "corrections": [
    { "type": "tense", "original": "I goes to school yesterday.", "corrected": "I went to school yesterday.", "reason": "Past tense required." }
  ],
  "level": "B",
  "audio_url": "/audio/abc123.mp3",
  "turns": 1,
  "pronunciation": { "score": 78, "speaking_rate_wpm": 120, "filler_count": 0 }
}
```

### `POST /api/chat_text`

Same as `/api/chat` but accepts a pre-transcribed text string instead of audio. Used by the streaming ASR path.

```json
{
  "session_id": "uuid",
  "transcription": "I went to the store.",
  "pronunciation": {},
  "voice": "en-US-AriaNeural",
  "prompt": ""
}
```

### `POST /api/speak`

Synthesizes text to speech and returns a URL.

```json
{ "text": "Hello, welcome back.", "voice": "en-US-AriaNeural" }
```

Response: `{ "audio_url": "/audio/abc.mp3", "voice": "en-US-AriaNeural" }`

### `POST /api/report`

Generates AI coach feedback for a session. Send either `session_id` or a direct summary payload.

```json
{ "session_id": "uuid" }
```

### `POST /api/live-feedback`

Generates a structured feedback card for a completed live-test session.

```json
{ "session_id": "uuid" }
```

Response: `{ "level": "B1", "overall": "...", "strengths": [], "improvements": [], "tip": "..." }`

### `GET /api/sessions`

Returns a list of all conversations ordered by most recent.

### `POST /api/sessions`

Creates a new empty session. Returns `{ "id": "uuid", ... }`.

### `GET /api/sessions/<session_id>`

Returns full session data including messages (with per-turn corrections embedded) and pronunciation scores.

### `PATCH /api/sessions/<session_id>/title`

Renames a conversation. `{ "title": "My Session" }`

### `DELETE /api/sessions/<session_id>`

Deletes a conversation and all its data.

### `WebSocket /ws/asr`

Streams raw audio chunks to Deepgram and pushes back partial and final transcripts as JSON frames.

## Example curl

```bash
curl -X POST http://127.0.0.1:5000/api/chat \
  -F "audio=@sample.webm"
```

```bash
curl -X POST http://127.0.0.1:5000/api/speak \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"Hello from SpeakAI\",\"voice\":\"en-US-AriaNeural\"}"
```

```bash
curl http://127.0.0.1:5000/api/sessions
```

## Tests

```powershell
pytest tests/test_api.py
```

The frontend smoke test is optional and skips automatically unless Playwright is installed:

```powershell
pytest tests/test_frontend_e2e.py
```

## Notes

- Local Whisper requires `ffmpeg` plus a Whisper-capable Python environment. The `whisper-exp` conda env is recommended.
- Server-side acoustic analysis (`services/acoustics.py`) also requires `ffmpeg` for volume stability metrics; the app continues without it if ffmpeg is unavailable.
- The Gemini integration uses the official `google-genai` SDK and supports either `GEMINI_API_KEY` or Vertex AI credentials.
- `edge-tts` writes synthesized files into `static/audio/` and serves them from `/audio/<filename>`.
- The system prompt can be customised per-request via the Settings page or the `prompt` field in API calls; punctuation-only corrections are suppressed by default since input comes from speech transcription.
