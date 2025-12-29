from __future__ import annotations

import random
import re
import discord
from discord import app_commands
from discord.ext import commands

DICE_RE = re.compile(r"^\s*(\d+)[dD](\d+)(?:\+(\d+))?\s*$")

class DiceRoller(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="roll", description="Roll dice: XdY or XdY+Z (e.g., 2d6+3)")
    async def roll(self, interaction: discord.Interaction, notation: str):
        m = DICE_RE.match(notation)
        if not m:
            return await interaction.response.send_message(
                "Use format `XdY` or `XdY+Z`, e.g. `2d6+1`.", ephemeral=True
            )
        x, y, z = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if x <= 0 or y <= 0 or x > 100 or y > 1000:
            return await interaction.response.send_message("Dice out of bounds.", ephemeral=True)
        rolls = [random.randint(1, y) for _ in range(x)]
        total = sum(rolls) + z
        detail = " + ".join(map(str, rolls)) + (f" + {z}" if z else "")
        await interaction.response.send_message(f"🎲 {notation} = **{total}** ({detail})")

async def setup(bot: commands.Bot):
    await bot.add_cog(DiceRoller(bot))
