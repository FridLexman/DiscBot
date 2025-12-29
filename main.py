from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Iterable

import discord
from discord.ext import commands

# === Logging Setup ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main")

# === Config Loading ===
CFG_PATH = os.getenv("CONFIG", "config.json")
p = Path(CFG_PATH)
if not p.exists():
    raise FileNotFoundError(
        f"Config file '{CFG_PATH}' not found. Create it from 'config.json.example'."
    )

config_raw = p.read_text(encoding="utf-8")
try:
    config: dict = json.loads(config_raw)
except json.JSONDecodeError as e:
    raise ValueError(f"config.json is not valid JSON: {e}") from e

if not isinstance(config, dict):
    raise ValueError("config.json must contain a top-level JSON object.")

# Optionally mirror config values to env for legacy code (no Riot keys anymore)
for k, v in config.items():
    if v is not None:
        os.environ[str(k)] = str(v)

# === Bot Setup ===
TOKEN = str(config.get("DISCORD_TOKEN", "")).strip()
PREFIX = str(config.get("PREFIX", ".")).strip() or "."
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in config.json")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = False  # flip to True if any cog needs member events

DEFAULT_EXTENSIONS: tuple[str, ...] = ("music", "clear", "diceroller", "meme")


class JakobyBot(commands.Bot):
    def __init__(self, *, extensions: Iterable[str], **kwargs):
        super().__init__(**kwargs)
        self._initial_extensions = tuple(extensions)

    async def setup_hook(self):
        await load_extensions(self, self._initial_extensions)
        try:
            await self.tree.sync()
            log.info("Application commands synced.")
        except Exception:
            log.exception("Failed to sync application commands.")


bot = JakobyBot(command_prefix=PREFIX, intents=intents, help_command=None, extensions=DEFAULT_EXTENSIONS)

# Expose a trimmed config to cogs if needed (no Riot content)
setattr(bot, "config", {k: v for k, v in config.items() if k not in {"RIOT_API_KEY", "RIOT_DEFAULT_PLATFORM"}})


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, getattr(bot.user, "id", "unknown"))


async def load_extensions(bot: commands.Bot, exts: Iterable[str]):
    for ext in exts:
        try:
            await bot.load_extension(ext)
            log.info("Loaded extension: %s", ext)
        except Exception:
            log.exception("Failed to load extension: %s", ext)


@bot.event
async def on_command_error(ctx: commands.Context, exc: commands.CommandError):
    # Minimal global error handler; cogs can override with local handlers
    if isinstance(exc, commands.CommandNotFound):
        return
    log.exception("Error in command '%s': %s", getattr(ctx.command, "qualified_name", "unknown"), exc)
    try:
        await ctx.reply("Something went wrong while running that command.", mention_author=False)
    except Exception:
        pass


async def main():
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
