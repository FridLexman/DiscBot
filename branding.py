from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import discord

_raw_path = os.getenv("BOT_LOGO_PATH", "assets/jakobys_bot.png")
_candidate = Path(_raw_path)
if _candidate.exists():
    LOGO_PATH: Optional[Path] = _candidate
else:
    LOGO_PATH = None

LOGO_ATTACHMENT_NAME = os.getenv("BOT_LOGO_ATTACHMENT_NAME")
if LOGO_ATTACHMENT_NAME:
    LOGO_ATTACHMENT_NAME = LOGO_ATTACHMENT_NAME.strip()
if not LOGO_ATTACHMENT_NAME and LOGO_PATH:
    LOGO_ATTACHMENT_NAME = LOGO_PATH.name

LOGO_URL = (os.getenv("BOT_LOGO_URL") or "").strip()
if not LOGO_URL:
    LOGO_URL = None


def logo_embed_url() -> Optional[str]:
    if LOGO_PATH and LOGO_ATTACHMENT_NAME:
        return f"attachment://{LOGO_ATTACHMENT_NAME}"
    if LOGO_URL:
        return LOGO_URL
    return None


def logo_requires_attachment() -> bool:
    return LOGO_PATH is not None and LOGO_ATTACHMENT_NAME is not None


def build_logo_file() -> Optional[discord.File]:
    if LOGO_PATH and LOGO_ATTACHMENT_NAME:
        return discord.File(LOGO_PATH, filename=LOGO_ATTACHMENT_NAME)
    return None
