from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from branding import build_logo_file, logo_embed_url

LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or "http://ollama.llm.svc.cluster.local:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.2:1b")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "200"))

class Meme(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._llm_metrics_lock = asyncio.Lock()
        self._llm_inflight = 0
        self._llm_last_latency: Optional[float] = None
        self._llm_last_success: Optional[datetime] = None
        self._llm_last_error: Optional[str] = None

    def _profile_snapshot(self, member: Optional[discord.Member], user: discord.abc.User) -> str:
        display = user.display_name
        parts = [f"handle: {display}"]
        if member:
            if member.nick and member.nick != display:
                parts.append(f"aka {member.nick}")
            if member.joined_at:
                days = (datetime.now(timezone.utc) - member.joined_at).days
                parts.append(f"in server for {days} days")
            role_names = [r.name for r in getattr(member, "roles", []) if getattr(r, "name", "@everyone") != "@everyone"]
            if role_names:
                parts.append(f"roles: {', '.join(role_names[:5])}")
            top_role = getattr(member, "top_role", None)
            if top_role and top_role.name not in ("@everyone",) and top_role.name not in role_names[:5]:
                parts.append(f"notable rank: {top_role.name}")
        return "; ".join(parts)

    async def _run_llm_chat(self, *, system_prompt: str, user_prompt: str, temperature: float = 0.8) -> Optional[str]:
        if not LLM_BASE_URL or not LLM_MODEL:
            return None

        await self._change_inflight(1)
        start = time.perf_counter()

        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": LLM_MAX_TOKENS},
        }

        timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{LLM_BASE_URL}/api/chat", json=payload) as resp:
                    if resp.status != 200:
                        await self._record_llm_error(f"HTTP {resp.status}")
                        await self._change_inflight(-1)
                        return None
                    data = await resp.json()
        except Exception as exc:
            await self._record_llm_error(str(exc))
            await self._change_inflight(-1)
            return None
        await self._change_inflight(-1)

        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content") or ""
            await self._record_llm_success(time.perf_counter() - start)
            return content.strip()
        content = data.get("response") or ""
        if content:
            await self._record_llm_success(time.perf_counter() - start)
        else:
            await self._record_llm_error("Empty response")
        return content.strip() or None

    async def _reply_llm_error(self, interaction: discord.Interaction):
        message = "LLM service is offline. Set LLM_BASE_URL/LLM_MODEL env vars and restart the bot."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="joke", description="Tell an anti-government joke")
    async def joke(self, interaction: discord.Interaction):
        if not LLM_BASE_URL:
            return await self._reply_llm_error(interaction)

        await interaction.response.defer(thinking=True)

        system_prompt = (
            "You host a mischievous late-night satire show that loves teasing red tape, budget hearings, and bureaucratic chaos."
            " Your humor is clever, PG-13, and feels like insider banter between civic hackers—never hateful or violent."
            " When asked for a joke, you always deliver a fresh quip instead of refusing."
        )
        user_prompt = (
            "Tell one original joke about bloated bureaucracy, paperwork, or surveillance theater."
            " Keep it witty, lighthearted, and short enough to fit in a Discord message."
        )
        content = await self._run_llm_chat(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.9)
        if not content:
            return await self._reply_llm_error(interaction)

        if len(content) > 1900:
            content = content[:1900] + "…"

        await interaction.followup.send(content)

    @app_commands.command(name="bully", description="Drop a vicious roast on someone (or yourself)")
    @app_commands.describe(target="Victim that gets roasted; defaults to you")
    async def bully(self, interaction: discord.Interaction, target: Optional[discord.User] = None):
        target = target or interaction.user
        if not LLM_BASE_URL:
            return await self._reply_llm_error(interaction)

        await interaction.response.defer(thinking=True)

        member: Optional[discord.Member] = None
        if interaction.guild:
            member = interaction.guild.get_member(target.id)
            if not member:
                try:
                    member = await interaction.guild.fetch_member(target.id)  # type: ignore
                except discord.HTTPException:
                    member = None

        dossier = self._profile_snapshot(member, target)

        system_prompt = (
            "You are a charismatic roastmaster for a Discord variety show."
            " Every roast stays playful, witty, and anchored in observable quirks—never hateful, violent, or mean-spirited."
            " You always respond with a clever roast when prompted."
        )
        user_prompt = (
            f"Deliver a playful roast of {target.display_name} ({target.mention}) like a shoutcaster hyping a teammate."
            f" Thread in these dossier notes: {dossier}."
            " Stay affectionate, under four sentences, and focus on friendly rivalry—not hate or harm."
        )
        content = await self._run_llm_chat(system_prompt=system_prompt, user_prompt=user_prompt, temperature=1.0)
        if not content:
            return await self._reply_llm_error(interaction)

        if len(content) > 1900:
            content = content[:1900] + "…"

        allowed = discord.AllowedMentions(users=[target])
        await interaction.followup.send(content, allowed_mentions=allowed)

    @app_commands.command(name="llmstatus", description="Show the internal LLM status dashboard")
    async def llmstatus(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        diagnostics = await self._gather_llm_diagnostics()
        embed = self._build_llm_embed(diagnostics)
        file = build_logo_file()
        await interaction.followup.send(embed=embed, ephemeral=True, file=file)

    async def _change_inflight(self, delta: int):
        async with self._llm_metrics_lock:
            self._llm_inflight = max(0, self._llm_inflight + delta)

    async def _record_llm_success(self, duration: float):
        async with self._llm_metrics_lock:
            self._llm_last_latency = duration
            self._llm_last_success = datetime.now(timezone.utc)
            self._llm_last_error = None

    async def _record_llm_error(self, message: str):
        async with self._llm_metrics_lock:
            self._llm_last_error = message

    async def _snapshot_metrics(self):
        async with self._llm_metrics_lock:
            return {
                "inflight": self._llm_inflight,
                "last_latency": self._llm_last_latency,
                "last_success": self._llm_last_success,
                "last_error": self._llm_last_error,
            }

    async def _gather_llm_diagnostics(self) -> dict:
        metrics = await self._snapshot_metrics()
        summary_lines: list[str] = []
        reachable = False
        tags: list[str] = []

        if not LLM_BASE_URL:
            summary_lines.append("❌ LLM_BASE_URL is not configured.")
            return {"reachable": False, "logs": summary_lines, "metrics": metrics, "models": []}

        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            reachable = await self._ping_llm(session, summary_lines)
            if not reachable:
                await self._wake_llm(session, summary_lines)
                reachable = await self._ping_llm(session, summary_lines, post_wake=True)

            if reachable:
                tags = await self._fetch_llm_models(session, summary_lines)

        return {"reachable": reachable, "logs": summary_lines, "metrics": metrics, "models": tags}

    async def _ping_llm(
        self, session: aiohttp.ClientSession, logs: list[str], post_wake: bool = False
    ) -> bool:
        if not post_wake:
            logs.append("📡 Hailing JakobyAI uplink…")
        try:
            async with session.get(f"{LLM_BASE_URL}/") as resp:
                if resp.status == 200:
                    logs.append("🟢 JakobyAI responded with a green heartbeat.")
                    return True
                logs.append(f"⚠️ Control room flashed HTTP {resp.status}.")
        except Exception as exc:
            logs.append(f"⚠️ No answer: {exc}.")
        return False

    async def _wake_llm(self, session: aiohttp.ClientSession, logs: list[str]):
        logs.extend(
            [
                "⚡ Dispatching a contraband WoL burst via the scav relay…",
                "⏳ Waiting up to 20s for the contraband Ollama crate to boot…",
            ]
        )
        await asyncio.sleep(2)
        try:
            async with session.post(f"{LLM_BASE_URL}/api/ps") as resp:
                if resp.status == 200:
                    logs.append("🧰 Supervisor acknowledged the spin-up request.")
                else:
                    logs.append(f"⚠️ Supervisor refused with HTTP {resp.status}.")
        except Exception as exc:
            logs.append(f"⚠️ Could not reach supervisor: {exc}.")
        await asyncio.sleep(1)

    async def _fetch_llm_models(self, session: aiohttp.ClientSession, logs: list[str]) -> list[str]:
        try:
            async with session.get(f"{LLM_BASE_URL}/api/tags") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [entry.get("name") for entry in data.get("models", []) if entry.get("name")]
                    logs.append(f"📦 Loaded manifests: {', '.join(models) if models else 'none'}")
                    return models
                logs.append(f"⚠️ Could not enumerate manifests (HTTP {resp.status}).")
        except Exception as exc:
            logs.append(f"⚠️ Could not enumerate manifests: {exc}.")
        return []

    def _build_llm_embed(self, diagnostics: dict) -> discord.Embed:
        reachable = diagnostics["reachable"]
        logs = diagnostics["logs"]
        metrics = diagnostics["metrics"]
        models = diagnostics["models"]

        color = discord.Color.green() if reachable else discord.Color.red()
        embed = discord.Embed(
            title="Jakoby AI Ops Console",
            description="\n".join(logs) or "No telemetry collected.",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(name="LLM Status", value="ONLINE ✅" if reachable else "OFFLINE ❌", inline=True)
        embed.add_field(name="Active Queue", value=f"{metrics['inflight']} in-flight", inline=True)

        if metrics["last_latency"] is not None:
            embed.add_field(name="Last Response", value=f"{metrics['last_latency']:.2f}s", inline=True)
        if metrics["last_success"]:
            embed.add_field(
                name="Last Success",
                value=discord.utils.format_dt(metrics["last_success"], style="R"),
                inline=True,
            )
        if metrics["last_error"]:
            embed.add_field(name="Last Error", value=metrics["last_error"][:256], inline=False)

        model_text = ", ".join(models) if models else "No manifests detected"
        embed.add_field(name="Models", value=model_text, inline=False)

        logo_url = logo_embed_url()
        if logo_url:
            embed.set_thumbnail(url=logo_url)

        embed.set_footer(text="Automated by Jakoby's DiscBot • LLM telemetry")
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(Meme(bot))
