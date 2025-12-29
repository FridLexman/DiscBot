# DiscBot – Music & Utility Bot (Skeleton)  
Version: v15.4 (production parity)  

This repository contains a self‑hosted Discord music/utility bot skeleton with no secrets committed. It mirrors the current production codebase (v15.4) and is ready to configure with your own tokens and LLM endpoints.

## Features
- Music playback via yt-dlp + FFmpeg; supports YouTube links/search.
- Spotify URL resolver → plays via YouTube audio.
- Persistent control panel with buttons for play/pause/skip/repeat.
- Slash-only utilities: clear, dice roller, meme cog (LLM-backed `/joke`, `/bully`).
- Idle safeguards: disconnect if alone; 5-minute idle timeout when queue is empty.

## Quickstart
```bash
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\\Scripts\\activate
pip install -U pip
pip install -r requirements.txt
cp config.example.json config.json  # edit with your own tokens (keep this out of git)
python main.py
```

### config.json (example keys)
```json
{
  "DISCORD_TOKEN": "your_discord_bot_token",
  "PREFIX": "/",
  "FFMPEG_EXE": "ffmpeg",

  "SPOTIFY_CLIENT_ID": "",
  "SPOTIFY_CLIENT_SECRET": "",

  "OPENAI_API_KEY": "",
  "OPENAI_MODEL": "gpt-3.5-turbo",
  "OPENAI_BASE_URL": "https://api.openai.com/v1",

  "LLM_BASE_URL": "http://llm-service.local:11434",
  "LLM_MODEL": "llama3"
}
```
> Keep `config.json` and any tokens out of version control.

## Commands
- Music: `/play <query_or_url>`, pause/resume/skip/stop, queue display, repeat controls.
- Utilities: `/clear [n]`, `/roll <XdY[+Z]>`, `/joke`, `/bully`.

## Deployment Notes
- Dockerfile provided for container builds (FFmpeg included).
- Kubernetes manifests in `k8s/` for GitLab Runner and deploy pipeline.
- The GitLab CI template builds with Kaniko and can `kubectl set image` to roll out new tags.

## Security
- No secrets are tracked. Provide your own tokens/keys via `config.json` or environment.
- Enable secret scanning and push protection on your fork (GitHub settings).
