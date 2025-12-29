# ---- Base (slim + ffmpeg) ----
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps: ffmpeg (for voice), curl (healthcheck), gcc for wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl build-essential \
 && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m appuser
WORKDIR /app

# Copy only requirements first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . /app

# Security: drop to non-root
USER appuser

# The bot reads config.json and uses env like FFMPEG_EXE, SPOTIFY_*, OPENAI_*  :contentReference[oaicite:4]{index=4} :contentReference[oaicite:5]{index=5} :contentReference[oaicite:6]{index=6}
ENV FFMPEG_EXE=ffmpeg \
    CONFIG=config.json

# Healthcheck: is the process alive?
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD pgrep -f "python.*main.py" || exit 1

# Entrypoint
CMD ["python", "main.py"]
