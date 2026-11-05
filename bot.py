"""GameScope Discord bot entry point.

The /game command provides Steam and ProtonDB views; /settings configures the bot.
HTTP access lives in services.py and embed construction in embeds.py.
"""
from __future__ import annotations

import asyncio
import os
import logging
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
from models import (
    BadResponse,
    RateLimited,
    ResolvedGame,
    UpstreamError,
    UpstreamUnavailable,
)
from services import GameServices
from textutil import choice_label

log = config.configure_logging()

NO_MENTIONS = discord.AllowedMentions.none()
services = GameServices()


# ---------------------------------------------------------------------------
# interaction helpers
# ---------------------------------------------------------------------------
def short_reason(exc: BaseException | None) -> str | None:
    """Short, user-safe description of an upstream failure (details stay in the logs)."""
    if exc is None:
        return None
    if isinstance(exc, RateLimited):
        return f"{exc.service} is rate limiting requests"
    if isinstance(exc, UpstreamUnavailable):
        return f"{exc.service} did not respond properly"
    if isinstance(exc, BadResponse):
        return f"{exc.service} returned unexpected data"
    return "temporarily unavailable"


def describe_upstream(exc: UpstreamError) -> str:
    if isinstance(exc, RateLimited):
        wait = max(5, int(exc.retry_after))
        return f"{exc.service} is rate limiting requests right now. Please try again in about {wait} seconds."
    if isinstance(exc, UpstreamUnavailable):
        return f"{exc.service} didn't respond properly. Use `/ping` to check connectivity."
    if isinstance(exc, BadResponse):
        return f"{exc.service} returned unexpected data. Use `/ping` to check connectivity."
    return "A game-data service is temporarily unavailable. Use `/ping` to check connectivity."


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    """Acknowledge within Discord's 3 second deadline. False if that is no longer possible."""
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        return True
    except discord.NotFound:
        log.warning("Interaction %s expired before it could be acknowledged", interaction.id)
    except discord.HTTPException as exc:
        log.warning("Could not acknowledge interaction %s: %s", interaction.id, exc)
    return False


async def send_embed(
    interaction: discord.Interaction,
    embed: discord.Embed,
    *,
    ephemeral: bool = False,
    view: discord.ui.View | None = None,
) -> None:
    kwargs: dict[str, Any] = {"embed": embed, "ephemeral": ephemeral, "allowed_mentions": NO_MENTIONS}
    if view is not None:
        kwargs["view"] = view
    kwargs["embed"] = customise(interaction, embed)
    await interaction.followup.send(**kwargs)


async def send_error(
    interaction: discord.Interaction,
    message: str,
    *,
    title: str = "Something went wrong",
    was_public: bool,
) -> None:
    """Send an error embed privately.

    After a public ``defer`` the first follow-up would replace the public
    "thinking" message and be public too, so remove that placeholder first.
    """
    embed = embeds.error_embed(message, title)
    try:
        if was_public:
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException:
        log.exception("Could not deliver an error message for interaction %s", interaction.id)


async def capture(awaitable: Awaitable[Any]) -> tuple[Any, UpstreamError | None]:
    """Await ``awaitable``; return (result, None) or (None, upstream_error)."""
    try:
        return await awaitable, None
    except UpstreamError as exc:
        return None, exc


async def run_lookup(
    interaction: discord.Interaction,
    label: str,
    handler: Callable[[], Awaitable[None]],
    *,
    public: bool,
) -> None:
    """Defer immediately, run ``handler`` and turn any failure into a private error embed."""
    allowed = options(interaction)["allowed_channels"]
    if allowed and interaction.channel_id not in allowed:
        await interaction.response.send_message("This bot is restricted to other channels.", ephemeral=True)
        return
    if not await safe_defer(interaction, ephemeral=not public):
        return
    try:
        await handler()
    except ValueError as exc:
        await send_error(interaction, str(exc), was_public=public)
    except UpstreamError as exc:
        log.warning("%s failed: %s", label, exc)
        await send_error(interaction, describe_upstream(exc), was_public=public)
    except discord.HTTPException:
        log.exception("Discord rejected the response for %s", label)
        await send_error(interaction, "Discord rejected that response. It has been logged.", was_public=public)
    except Exception:
        log.exception("Unexpected error handling %s", label)
        await send_error(
            interaction,
            "Something went wrong while handling that request. It has been logged on the server.",
            was_public=public,
        )


async def need_game(interaction: discord.Interaction, query: str) -> ResolvedGame | None:
    resolved = await services.resolve_game(query)
    if resolved is None:
        await send_error(
            interaction,
            "I couldn't find that game on Steam. Try a more specific title, pick a suggestion from the "
            "autocomplete list, or use its Steam App ID.",
            title="Game not found",
            was_public=True,
        )
    return resolved


async def resolve_input(
    interaction: discord.Interaction,
    game: str | None,
    appid: int | None,
) -> ResolvedGame | None:
    """Prefer an explicit App ID; otherwise resolve the supplied game name."""
    if appid is not None:
        return ResolvedGame(appid, services.known_name(appid))
    if game and game.strip():
        return await need_game(interaction, game)
    await send_error(
        interaction,
        "Provide either a game name or a Steam App ID.",
        title="Missing game",
        was_public=True,
    )
    return None


# ---------------------------------------------------------------------------
# shared /game autocomplete
# ---------------------------------------------------------------------------
async def game_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = (current or "").strip()
    choices: list[app_commands.Choice[str]] = []
    seen: set[str] = set()

    if current.isdigit() and 0 < int(current) <= config.MAX_STEAM_APPID:
        choices.append(app_commands.Choice(name=f"Look up Steam App ID {current}"[: config.CHOICE_NAME_MAX], value=current))
        seen.add(current)

    if len(current) >= 2:
        try:
            results = await services.search_games(current, fast=True)
        except UpstreamError as exc:
            log.info("Autocomplete search unavailable: %s", exc)
            results = []
        except Exception:
            log.exception("Unexpected autocomplete error")
            results = []
        for item in results:
            value = str(item.appid)
            if value in seen:
                continue
            seen.add(value)
            choices.append(app_commands.Choice(name=choice_label(item.name, item.appid, config.CHOICE_NAME_MAX), value=value))
    return choices[: config.CHOICE_LIMIT]


# ---------------------------------------------------------------------------
# /game buttons (stateless: the App ID lives in the custom_id, so they keep
# working after a bot restart and each click sends a NEW private embed)
# ---------------------------------------------------------------------------
async def send_requirements(interaction: discord.Interaction, appid: int) -> None:
    app = await services.steam_app(appid)
    if app is None:
        await send_error(
            interaction,
            "Steam has no store data for this game, so there are no requirements to show.",
            title="Requirements unavailable",
            was_public=False,
        )
        return
    await send_embed(interaction, embeds.build_requirements_embed(app), ephemeral=True)


async def send_reviews(interaction: discord.Interaction, appid: int) -> None:
    (summary, s_exc), (reviews, r_exc), (app, _a_exc) = await asyncio.gather(
        capture(services.review_summary(appid)),
        capture(services.recent_reviews(appid)),
        capture(services.steam_app(appid)),
    )
    name = (app.name if app else None) or services.known_name(appid) or f"Steam App {appid}"
    embed = embeds.build_reviews_embed(
        name, appid, summary, s_exc is not None, reviews, r_exc is not None
    )
    await send_embed(interaction, embed, ephemeral=True)


class RequirementsButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"steam:req:(?P<appid>[0-9]+)"
):
    def __init__(self, appid: int):
        super().__init__(
            discord.ui.Button(
                label="Requirements",
                style=discord.ButtonStyle.secondary,
                custom_id=f"steam:req:{appid}",
            )
        )
        self.appid = appid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["appid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await run_lookup(
            interaction,
            f"Requirements button (app {self.appid})",
            lambda: send_requirements(interaction, self.appid),
            public=False,
        )


class ReviewsButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"steam:rev:(?P<appid>[0-9]+)"
):
    def __init__(self, appid: int):
        super().__init__(
            discord.ui.Button(
                label="Reviews",
                style=discord.ButtonStyle.secondary,
                custom_id=f"steam:rev:{appid}",
            )
        )
        self.appid = appid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["appid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await run_lookup(
            interaction,
            f"Reviews button (app {self.appid})",
            lambda: send_reviews(interaction, self.appid),
            public=False,
        )


def add_links(view: discord.ui.View, appid: int, *, proton: bool = False, opts: dict | None = None) -> None:
    opts = opts or {}
    links = [
        ("Steam Store", f"https://store.steampowered.com/app/{appid}/"),
        ("ProtonDB Reports" if proton else "ProtonDB", f"https://www.protondb.com/app/{appid}"),
    ]
    if opts.get("steamdb_link", True):
        links.append(("SteamDB", f"https://steamdb.info/app/{appid}/"))
    if opts.get("pcgamingwiki_link", True):
        links.append(("PCGamingWiki", f"https://www.pcgamingwiki.com/w/index.php?search={appid}"))
    for label, url in links:
        view.add_item(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link))


def proton_buttons_view(appid: int, opts: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    add_links(view, appid, proton=True, opts=opts)
    return view


def steam_buttons_view(appid: int, opts: dict | None = None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RequirementsButton(appid))
    view.add_item(ReviewsButton(appid))
    add_links(view, appid, proton=False, opts=opts)
    return view


# ---------------------------------------------------------------------------
# bot
# ---------------------------------------------------------------------------
class GameScopeBot(commands.Bot):
    async def setup_hook(self) -> None:
        await services.start()
        self.add_dynamic_items(RequirementsButton, ReviewsButton, GameViewSelect, GameNewsButton)
        await self.sync_slash_commands()

    async def apply_saved_presence(self):
        p = settings.get(0)
        status = getattr(discord.Status, p["presence_status"], discord.Status.online)
        kind = p["presence_type"]
        name = p["presence_text"].strip()
        activity = None
        if p["presence_enabled"] and name:
            activity = discord.Game(name=name) if kind == "playing" else discord.Activity(type=getattr(discord.ActivityType, kind), name=name)
        await self.change_presence(status=status, activity=activity)

    async def sync_slash_commands(self) -> None:
        try:
            if config.GUILD_ID.isdigit():
                guild = discord.Object(id=int(config.GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                where = f"test guild {config.GUILD_ID}"
            else:
                synced = await self.tree.sync()
                where = "all servers (global)"
            names = ", ".join("/" + c.name for c in sorted(synced, key=lambda c: c.name))
            log.info("Synced %s slash command(s) to %s: %s", len(synced), where, names)
        except discord.Forbidden:
            log.error(
                "Discord refused the slash-command sync (403). Check DISCORD_GUILD_ID and that the bot was "
                "invited with the 'applications.commands' scope."
            )
        except discord.HTTPException:
            log.exception("Slash-command sync failed; the bot will keep running with the previously registered commands")

    async def close(self) -> None:
        await services.close()
        await super().close()


bot = GameScopeBot(
    command_prefix=commands.when_mentioned,
    intents=discord.Intents.default(),
    allowed_mentions=NO_MENTIONS,
)


from settings_store import SettingsStore

settings = SettingsStore()
VISIBILITY_CHOICES = [
    app_commands.Choice(name="Public", value="Public"),
    app_commands.Choice(name="Private", value="Private"),
]

def options(interaction):
    return settings.get(interaction.guild_id)

def public_response(interaction, visibility):
    return (visibility or options(interaction)["visibility"]) == "Public"

def moderator(interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    p = interaction.user.guild_permissions
    allowed = options(interaction).get("settings_roles", [])
    return p.administrator or any(r.id in allowed for r in interaction.user.roles)

def customise(interaction, embed, appid=None):
    o = options(interaction)
    embed.colour = discord.Colour(int(o["colour"][1:], 16))
    return embeds.enforce_limits(embed)


class SettingsMenu(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=600)
        self.guild_id, self.user_id = guild_id, user_id

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.user_id or not moderator(interaction):
            await interaction.response.send_message("Only the authorised member who opened this menu can change settings.", ephemeral=True)
            return False
        return True

    def render(self):
        o = settings.get(self.guild_id)
        guild = bot.get_guild(self.guild_id)
        roles = [guild.get_role(rid) for rid in o.get("settings_roles", [])] if guild else []
        role_name = ", ".join(discord.utils.escape_markdown(r.name) for r in roles if r) or "Administrators only"
        channel_names = []
        for channel_id in o["allowed_channels"]:
            channel = guild.get_channel(channel_id) if guild else None
            channel_names.append("#" + discord.utils.escape_markdown(channel.name) if channel else f"Unknown channel ({channel_id})")
        e = discord.Embed(title="GameScope settings", description="Server-wide settings · Authorised Members Only", colour=int(o["colour"][1:], 16))
        e.add_field(name="Appearance", value=f'Default visibility: **{o["visibility"]}**\nEmbed colour: **{o["colour"]}**', inline=False)
        e.add_field(name="Game information", value=(
            f'ProtonDB reports: **{o["report_count"]}**\n'
            f'Steam news articles: **{o["news_count"]}**\n'
            f'SteamDB button: **{"On" if o["steamdb_link"] else "Off"}**\n'
            f'PCGamingWiki button: **{"On" if o["pcgamingwiki_link"] else "Off"}**'
        ), inline=False)
        e.add_field(name="Permissions", value=f'Settings access: **{role_name}**\nAllowed channels: **{", ".join(channel_names) or "All channels"}**', inline=False)
        return e

    @discord.ui.button(label="Visibility", style=discord.ButtonStyle.primary)
    async def visibility(self, interaction, button):
        o = settings.get(self.guild_id)
        settings.update(self.guild_id, visibility="Private" if o["visibility"] == "Public" else "Public")
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Link buttons", style=discord.ButtonStyle.secondary)
    async def embed_features(self, interaction, button):
        await interaction.response.send_message("Choose a feature to toggle:", view=FeatureMenu(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Content counts", style=discord.ButtonStyle.secondary)
    async def counts(self, interaction, button):
        await interaction.response.send_message("Choose a setting:", view=CountMenu(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Access", style=discord.ButtonStyle.secondary)
    async def access(self, interaction, button):
        await interaction.response.send_message("Choose an authorised settings role or allowed channels. Clearing a selection resets it.", view=AccessMenu(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Bot presence", style=discord.ButtonStyle.secondary, row=1)
    async def presence(self, interaction, button):
        if not moderator(interaction):
            await interaction.response.send_message("Only administrators and authorised settings roles can change presence.", ephemeral=True)
            return
        await interaction.response.send_message(embed=presence_embed(), view=PresenceMenu(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Colour", style=discord.ButtonStyle.secondary)
    async def colour(self, interaction, button):
        await interaction.response.send_modal(ColourModal(self.guild_id))


class SubMenu(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=300)
        self.guild_id, self.user_id = guild_id, user_id

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.user_id or not moderator(interaction):
            await interaction.response.send_message("Not authorised.", ephemeral=True)
            return False
        return True

    async def save(self, interaction, **changes):
        settings.update(self.guild_id, **changes)
        await interaction.response.edit_message(content="Saved. Run `/settings` to see the updated overview.", view=None)


class FeatureMenu(SubMenu):
    @discord.ui.select(placeholder="Toggle a link button", options=[
        discord.SelectOption(label="PCGamingWiki button", value="pcgamingwiki_link"),
        discord.SelectOption(label="SteamDB button", value="steamdb_link"),
    ])
    async def choose(self, interaction, select):
        key = select.values[0]
        await self.save(interaction, **{key: not settings.get(self.guild_id)[key]})


class CountMenu(SubMenu):
    @discord.ui.select(placeholder="Choose how much content to show", options=[
        discord.SelectOption(label=f"ProtonDB reports: {n}", value=f"report_count:{n}") for n in (3, 5, 10)
    ] + [
        discord.SelectOption(label=f"Steam news articles: {n}", value=f"news_count:{n}") for n in (3, 5, 10)
    ])
    async def choose(self, interaction, select):
        key, value = select.values[0].split(":")
        await self.save(interaction, **{key: int(value)})


class AccessMenu(SubMenu):
    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Additional settings roles (none = admins only)", min_values=0, max_values=10)
    async def choose_role(self, interaction, select):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can grant settings access.", ephemeral=True)
            return
        await self.save(interaction, settings_roles=[r.id for r in select.values])

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Allowed channels (none = unrestricted)", min_values=0, max_values=10)
    async def choose_channels(self, interaction, select):
        await self.save(interaction, allowed_channels=[c.id for c in select.values])


def presence_embed():
    p = settings.get(0)
    e = discord.Embed(title="GameScope presence", description="Global bot presence · Authorised Members Only. Changes affect every server using this bot; administrators and authorised settings roles in any server can change it.", colour=0xB45D1F)
    e.add_field(name="Status", value=p["presence_status"].title(), inline=True)
    e.add_field(name="Activity", value=p["presence_type"].title() if p["presence_enabled"] else "Disabled", inline=True)
    e.add_field(name="Text", value=p["presence_text"] or "None", inline=False)
    return e


class PresenceTextModal(discord.ui.Modal, title="Activity text"):
    activity_text = discord.ui.TextInput(label="Activity text", placeholder="Steam games", max_length=120)

    async def on_submit(self, interaction):
        if not moderator(interaction):
            await interaction.response.send_message("Not authorised.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        settings.update(0, presence_text=str(self.activity_text.value))
        try:
            await bot.apply_saved_presence()
        except Exception:
            log.exception("Failed to apply presence text")
            await interaction.followup.send("Saved, but Discord could not apply the presence. Check logs.", ephemeral=True)
            return
        await interaction.followup.send("Presence text saved.", ephemeral=True)


class PresenceMenu(SubMenu):
    async def interaction_check(self, interaction):
        if not moderator(interaction):
            await interaction.response.send_message("Only administrators and authorised settings roles can change presence.", ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    async def save_presence(self, interaction, **changes):
        await interaction.response.defer()
        settings.update(0, **changes)
        try:
            await bot.apply_saved_presence()
        except Exception:
            log.exception("Failed to apply presence settings")
            await interaction.followup.send("Saved, but Discord could not apply the presence. Check logs.", ephemeral=True)
            return
        await interaction.edit_original_response(embed=presence_embed(), view=self)

    @discord.ui.select(placeholder="Bot status", options=[
        discord.SelectOption(label="Online", value="online"),
        discord.SelectOption(label="Idle", value="idle"),
        discord.SelectOption(label="Do Not Disturb", value="dnd"),
        discord.SelectOption(label="Invisible", value="invisible"),
    ])
    async def status_select(self, interaction, select):
        await self.save_presence(interaction, presence_status=select.values[0])

    @discord.ui.select(placeholder="Activity type", options=[
        discord.SelectOption(label="Playing", value="playing"),
        discord.SelectOption(label="Watching", value="watching"),
        discord.SelectOption(label="Listening", value="listening"),
        discord.SelectOption(label="Competing", value="competing"),
    ])
    async def activity_select(self, interaction, select):
        await self.save_presence(interaction, presence_type=select.values[0])

    @discord.ui.button(label="Change text", style=discord.ButtonStyle.primary)
    async def text_button(self, interaction, button):
        await interaction.response.send_modal(PresenceTextModal())

    @discord.ui.button(label="Enable / disable activity", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction, button):
        await self.save_presence(interaction, presence_enabled=not settings.get(0)["presence_enabled"])


class ColourModal(discord.ui.Modal, title="Embed colour"):
    colour = discord.ui.TextInput(label="Hex colour", placeholder="#B45D1F", min_length=6, max_length=7)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction):
        import re
        value = str(self.colour.value).strip()
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
            await interaction.response.send_message("Enter a valid six-digit hex colour.", ephemeral=True)
            return
        settings.update(self.guild_id, colour="#" + value.lstrip("#").upper())
        await interaction.response.send_message("Colour saved.", ephemeral=True)


@bot.tree.command(name="ping", description="Check Steam and ProtonDB connectivity.")
async def ping_command(interaction: discord.Interaction):
    """Check upstream reachability from the bot server."""
    import time
    import aiohttp
    if not await safe_defer(interaction, ephemeral=True):
        return

    async def probe(name: str, url: str) -> str:
        started = time.monotonic()
        try:
            if services.session is None or services.session.closed:
                return f"🔴 **{name}:** HTTP client unavailable"
            async with services.session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as response:
                elapsed = round((time.monotonic() - started) * 1000)
                if response.status < 400:
                    return f"🟢 **{name}:** Reachable ({elapsed} ms)"
                return f"🔴 **{name}:** HTTP {response.status} ({elapsed} ms)"
        except asyncio.TimeoutError:
            return f"🔴 **{name}:** Timed out"
        except aiohttp.ClientError as exc:
            log.warning("Connectivity check failed for %s: %s", name, exc)
            return f"🔴 **{name}:** Unreachable"

    results = await asyncio.gather(
        probe("Steam", "https://store.steampowered.com/api/appdetails?appids=620"),
        probe("ProtonDB", "https://www.protondb.com/api/v1/reports/summaries/620.json"),
    )
    await interaction.followup.send("\n".join(results), ephemeral=True)


@bot.tree.command(name="settings", description="Configure server-wide bot settings (Authorised Members Only).")
@app_commands.guild_only()
async def settings_command(interaction: discord.Interaction):
    if not moderator(interaction):
        await interaction.response.send_message("Authorised Members Only.", ephemeral=True)
        return
    menu = SettingsMenu(interaction.guild_id, interaction.user.id)
    await interaction.response.send_message(embed=menu.render(), view=menu, ephemeral=True)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    try:
        await bot.apply_saved_presence()
    except Exception:
        log.exception("Could not restore saved Discord presence")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    original = getattr(error, "original", error)
    if isinstance(original, discord.NotFound):
        log.info("Interaction %s expired before Discord accepted our reply", interaction.id)
        return
    log.error("Unhandled app command error", exc_info=error)
    if interaction.type is discord.InteractionType.autocomplete:
        return
    embed = embeds.error_embed("Something went wrong while handling that command. It has been logged on the server.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# /game: one command, selectable ProtonDB / Steam views, private news button
# ---------------------------------------------------------------------------
async def build_game_embed(interaction: discord.Interaction, appid: int, view_name: str):
    opts = options(interaction)
    if view_name == "Steam":
        (app, app_exc), (summary, summary_exc), (updates, _) = await asyncio.gather(
            capture(services.steam_app(appid)),
            capture(services.review_summary(appid)),
            capture(services.news(appid, count=50)),
        )
        if app_exc:
            raise app_exc
        if app is None:
            raise ValueError("Steam has no store information for this game.")
        embed = embeds.build_steam_embed(app, summary, reviews_unavailable=summary_exc is not None)
        latest = next((item for item in (updates or []) if is_game_update(item)), None)
        if latest:
            link = embeds.news_link(latest, appid)
            date = f"<t:{latest.date}:D>" if latest.date else "Date unknown"
            embed.add_field(name="Latest game update", value=f"**{embeds.safe_text(latest.title)}** · {date}" +
                            (f"\n[Read patch notes]({link})" if link else ""), inline=False)
        else:
            embed.add_field(name="Latest game update", value="No clearly labelled game update found in recent Steam news.", inline=False)
    else:
        (app, app_exc), (summary, summary_exc), reports = await asyncio.gather(
            capture(services.steam_app(appid)),
            capture(services.protondb_summary(appid)),
            services.protondb_reports(appid, limit=opts["report_count"]),
        )
        if app is None and app_exc is None and summary is None and summary_exc is None:
            raise ValueError("No Steam or ProtonDB information was found for this game.")
        name = (app.name if app else None) or services.known_name(appid) or f"Steam App {appid}"
        embed = embeds.build_protondb_embed(appid, name, summary, short_reason(summary_exc), reports, app)
    return customise(interaction, embed, appid)


def is_game_update(item) -> bool:
    import re
    return bool(re.search(
        r"patch\\s*notes?|hot\\s*fix|updates?|changelog|release notes|version\\s*[0-9]|"
        r"v[0-9]+(?:\\.[0-9]+)+|maintenance|bug\\s*fix|game\\s*fixes",
        item.title or "", re.I,
    ))


async def send_game_news(interaction: discord.Interaction, appid: int):
    (app, _), (items, error) = await asyncio.gather(
        capture(services.steam_app(appid)),
        capture(services.news(appid, count=50)),
    )
    if error:
        raise error
    name = (app.name if app else None) or services.known_name(appid) or f"Steam App {appid}"
    # Steam does not supply reliable per-article language metadata. Avoid rejecting valid titles.
    await send_embed(interaction, embeds.build_news_embed(name, appid, (items or [])[:options(interaction)["news_count"]]), ephemeral=True)


class GameViewSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"game:view:(?P<appid>[0-9]+)"):
    def __init__(self, appid: int):
        select = discord.ui.Select(
            placeholder="Choose game view",
            options=[
                discord.SelectOption(label="ProtonDB", value="ProtonDB", description="Linux compatibility and reports"),
                discord.SelectOption(label="Steam", value="Steam", description="Store information and latest update"),
            ],
            custom_id=f"game:view:{appid}",
            min_values=1, max_values=1,
        )
        super().__init__(select)
        self.appid = appid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["appid"]))

    async def callback(self, interaction: discord.Interaction):
        selected = self.item.values[0]
        await interaction.response.defer()
        try:
            embed = await build_game_embed(interaction, self.appid, selected)
            await interaction.message.edit(embed=embed, view=game_view(self.appid, selected, options(interaction)))
        except Exception:
            log.exception("Failed switching game view")
            await interaction.followup.send("Couldn't switch the game view. Check the bot logs.", ephemeral=True)


class GameNewsButton(discord.ui.DynamicItem[discord.ui.Button], template=r"game:news:(?P<appid>[0-9]+)"):
    def __init__(self, appid: int):
        super().__init__(discord.ui.Button(label="News", style=discord.ButtonStyle.secondary, custom_id=f"game:news:{appid}"))
        self.appid = appid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["appid"]))

    async def callback(self, interaction):
        await run_lookup(interaction, f"News button {self.appid}", lambda: send_game_news(interaction, self.appid), public=False)


def game_view(appid: int, selected: str, opts: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(GameViewSelect(appid))
    if selected == "Steam":
        view.add_item(RequirementsButton(appid))
        view.add_item(ReviewsButton(appid))
    if selected == "Steam":
        view.add_item(GameNewsButton(appid))
    add_links(view, appid, proton=selected != "Steam", opts=opts)
    return view


@bot.tree.command(name="game", description="Look up a game on ProtonDB or Steam.")
@app_commands.describe(game="Game name or Steam App ID (required)", view="Choose ProtonDB or Steam", visibility="Public or private response")
@app_commands.choices(
    view=[
        app_commands.Choice(name="ProtonDB", value="ProtonDB"),
        app_commands.Choice(name="Steam", value="Steam"),
    ],
    visibility=VISIBILITY_CHOICES,
)
@app_commands.autocomplete(game=game_autocomplete)
async def game_command(
    interaction: discord.Interaction,
    game: str,
    view: str | None = None,
    visibility: str | None = None,
):
    async def handler():
        resolved = await need_game(interaction, game)
        if resolved is None:
            return
        chosen = view or "ProtonDB"
        embed = await build_game_embed(interaction, resolved.appid, chosen)
        await send_embed(interaction, embed, view=game_view(resolved.appid, chosen, options(interaction)))
    await run_lookup(interaction, f"/game game={game!r}", handler, public=public_response(interaction, visibility))



def main() -> None:
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set it in your .env file.")
    # log_handler=None: logging is already configured in config.configure_logging()
    bot.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
