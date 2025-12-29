from __future__ import annotations

from discord import app_commands, Interaction
from discord.ext import commands

class Clear(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="clear", description="Clear last N messages (default 10)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def slash_clear(self, interaction: Interaction, amount: int = 10):
        amount = max(1, min(200, amount))
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if channel is None:
            return await interaction.followup.send("No channel found.", ephemeral=True)
        try:
            deleted = await channel.purge(limit=amount)
        except Exception as exc:
            return await interaction.followup.send(f"Failed to clear messages: {exc}", ephemeral=True)
        await interaction.followup.send(f"Cleared {len(deleted)} messages.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Clear(bot))
