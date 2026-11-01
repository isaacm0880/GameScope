"""ProtonDB / Steam Discord bot - entry point.

Commands: /protondb, /steam (with Requirements and Reviews buttons), and /news.
Each command accepts a game name or Steam App ID. HTTP access lives in services.py, embed construction in
embeds.py, parsing in parsers.py.
"""
from __future__ import annotations

import asyncio
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
        return f"{exc.service} didn't respond properly. Please try again in a minute."
    if isinstance(exc, BadResponse):
        return f"{exc.service} returned data in a format I didn't expect. Please try again later."
    return "A game-data service is temporarily unavailable. Please try again shortly."


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
# shared autocomplete (used by /protondb, /steam and /news)
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
# /steam buttons (stateless: the App ID lives in the custom_id, so they keep
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
class ProtonDBBot(commands.Bot):
    async def setup_hook(self) -> None:
        await services.start()
        self.add_dynamic_items(RequirementsButton, ReviewsButton)
        await self.sync_slash_commands()

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


bot = ProtonDBBot(
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
    role = options(interaction)["moderator_role"]
    return p.administrator or p.manage_guild or p.manage_messages or (
        role is not None and any(r.id == role for r in interaction.user.roles)
    )

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
            await interaction.response.send_message("Only the moderator who opened this menu can change settings.", ephemeral=True)
            return False
        return True

    def render(self):
        o = settings.get(self.guild_id)
        e = discord.Embed(title="Bot settings", description="Server-wide settings · moderator access", colour=int(o["colour"][1:], 16))
        e.add_field(name="General", value=f'Default visibility: **{o["visibility"]}**\nColour: **{o["colour"]}**\nNews items: **{o["news_count"]}**', inline=False)
        e.add_field(name="Embed features", value="\n".join(f'{k}: **{"Enabled" if o[k] else "Disabled"}**' for k in ("steamdeck", "anticheat", "pcgamingwiki_link", "steamdb_link")), inline=False)
        e.add_field(name="Reports", value=f'Count: **{o["report_count"]}**\nNewest first: **{o["newest_first"]}**', inline=False)
        e.add_field(name="Access", value=f'Moderator role: **{o["moderator_role"] or "None"}**\nAllowed channels: **{", ".join(map(str, o["allowed_channels"])) or "All"}**', inline=False)
        return e

    @discord.ui.button(label="Visibility", style=discord.ButtonStyle.primary)
    async def visibility(self, interaction, button):
        o = settings.get(self.guild_id)
        settings.update(self.guild_id, visibility="Private" if o["visibility"] == "Public" else "Public")
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Embed features", style=discord.ButtonStyle.secondary)
    async def embed_features(self, interaction, button):
        await interaction.response.send_message("Choose a feature to toggle:", view=FeatureMenu(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Reports / news", style=discord.ButtonStyle.secondary)
    async def counts(self, interaction, button):
        await interaction.response.send_message("Choose a setting:", view=CountMenu(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Access", style=discord.ButtonStyle.secondary)
    async def access(self, interaction, button):
        await interaction.response.send_message("Select a moderator role or allowed channels. Clear the selection to reset.", view=AccessMenu(self.guild_id, self.user_id), ephemeral=True)

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
        await interaction.response.edit_message(content="Saved. Reopen /settings to see the updated overview.", view=None)


class FeatureMenu(SubMenu):
    @discord.ui.select(placeholder="Toggle a feature", options=[
        discord.SelectOption(label="Steam Deck status", value="steamdeck"),
        discord.SelectOption(label="Anti-cheat status", value="anticheat"),
        discord.SelectOption(label="PCGamingWiki links", value="pcgamingwiki_link"),
        discord.SelectOption(label="SteamDB links", value="steamdb_link"),
    ])
    async def choose(self, interaction, select):
        key = select.values[0]
        await self.save(interaction, **{key: not settings.get(self.guild_id)[key]})


class CountMenu(SubMenu):
    @discord.ui.select(placeholder="Choose a count or report order", options=[
        discord.SelectOption(label=f"Reports: {n}", value=f"report_count:{n}") for n in (3, 5, 10)
    ] + [
        discord.SelectOption(label=f"News: {n}", value=f"news_count:{n}") for n in (3, 5, 10)
    ] + [discord.SelectOption(label="Toggle newest-first reports", value="newest_first:toggle")])
    async def choose(self, interaction, select):
        key, value = select.values[0].split(":")
        await self.save(interaction, **{key: not settings.get(self.guild_id)[key] if value == "toggle" else int(value)})


class AccessMenu(SubMenu):
    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Moderator role (optional)", min_values=0, max_values=1)
    async def choose_role(self, interaction, select):
        await self.save(interaction, moderator_role=select.values[0].id if select.values else None)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Allowed channels (none = unrestricted)", min_values=0, max_values=10)
    async def choose_channels(self, interaction, select):
        await self.save(interaction, allowed_channels=[c.id for c in select.values])


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


@bot.tree.command(name="settings", description="Configure server-wide bot settings (moderators only).")
@app_commands.guild_only()
async def settings_command(interaction: discord.Interaction):
    if not moderator(interaction):
        await interaction.response.send_message("Only moderators can change bot settings.", ephemeral=True)
        return
    menu = SettingsMenu(interaction.guild_id, interaction.user.id)
    await interaction.response.send_message(embed=menu.render(), view=menu, ephemeral=True)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")


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
# /protondb and /protondbid
# ---------------------------------------------------------------------------
async def show_protondb(interaction: discord.Interaction, resolved: ResolvedGame) -> None:
    appid = resolved.appid
    (app, _app_exc), (summary, summary_exc), reports = await asyncio.gather(
        capture(services.steam_app(appid)),
        capture(services.protondb_summary(appid)),
        services.protondb_reports(appid, limit=options(interaction)["report_count"]),
    )
    if app is None and _app_exc is None and summary is None and summary_exc is None:
        await send_error(
            interaction,
            f"I couldn't find Steam or ProtonDB data for App ID `{appid}`.",
            title="Game not found",
            was_public=True,
        )
        return
    name = (app.name if app else None) or resolved.name or services.known_name(appid) or f"Steam App {appid}"
    embed = embeds.build_protondb_embed(appid, name, summary, short_reason(summary_exc), reports, app)
    await send_embed(interaction, customise(interaction, embed, appid), view=proton_buttons_view(appid, options(interaction)))


@bot.tree.command(name="protondb", description="Check a game's Linux compatibility on ProtonDB.")
@app_commands.describe(game="Search by game name", appid="Or enter a numeric Steam App ID", visibility="Public or private response")
@app_commands.choices(visibility=VISIBILITY_CHOICES)
@app_commands.autocomplete(game=game_autocomplete)
async def protondb_command(
    interaction: discord.Interaction,
    game: str | None = None,
    appid: app_commands.Range[int, 1, 999999999] | None = None,
    visibility: str | None = None,
) -> None:
    async def handler() -> None:
        resolved = await resolve_input(interaction, game, appid)
        if resolved:
            await show_protondb(interaction, resolved)

    await run_lookup(interaction, f"/protondb game={game!r} appid={appid}", handler, public=public_response(interaction, visibility))


# ---------------------------------------------------------------------------
# /steam
# ---------------------------------------------------------------------------
async def show_steam(interaction: discord.Interaction, resolved: ResolvedGame) -> None:
    (app, app_exc), (summary, summary_exc), (updates, update_exc) = await asyncio.gather(
        capture(services.steam_app(resolved.appid)),
        capture(services.review_summary(resolved.appid)),
        capture(services.news(resolved.appid, count=100)),
    )
    if app_exc is not None:
        raise app_exc
    if app is None:
        await send_error(
            interaction,
            f"Steam has no store data for App ID `{resolved.appid}`. It may be delisted, "
            "region-restricted or not a store item.",
            title="No Steam data",
            was_public=True,
        )
        return
    embed = embeds.build_steam_embed(app, summary, reviews_unavailable=summary_exc is not None)
    latest = next((item for item in (updates or []) if is_game_update(item)), None)
    if latest:
        link = embeds.news_link(latest, app.appid)
        title = embeds.safe_text(latest.title)
        date = f"<t:{latest.date}:D>" if latest.date else "Date unknown"
        embed.add_field(name="Latest game update", value=f"**{title}** · {date}" + (f"\n[Read patch notes]({link})" if link else ""), inline=False)
    else:
        embed.add_field(name="Latest game update", value="No verified game update found in recent official Steam announcements.", inline=False)
    await send_embed(interaction, customise(interaction, embed, app.appid), view=steam_buttons_view(app.appid, options(interaction)))


def is_game_update(item) -> bool:
    import re
    if item.feedname != "steam_community_announcements" and "steam_community_announcements" not in (item.url or ""):
        return False
    if not is_english_news(item):
        return False
    return bool(re.search(r"patch notes|hotfix|update|changelog|release notes|version\\s+[0-9]|maintenance|fixes", item.title or "", re.I))


def is_english_news(item) -> bool:
    import re
    # Steam does not consistently label the language of aggregated news feeds.
    text = (item.title or "") + " " + (item.contents or "")[:350]
    return not bool(re.search(r"[\\u0400-\\u052f\\u0600-\\u06ff\\u4e00-\\u9fff\\u3040-\\u30ff\\uac00-\\ud7af]", text))


@bot.tree.command(name="steam", description="Show Steam store info: price, reviews, release date and more.")
@app_commands.describe(game="Search by game name", appid="Or enter a numeric Steam App ID", visibility="Public or private response")
@app_commands.choices(visibility=VISIBILITY_CHOICES)
@app_commands.autocomplete(game=game_autocomplete)
async def steam_command(
    interaction: discord.Interaction,
    game: str | None = None,
    appid: app_commands.Range[int, 1, 999999999] | None = None,
    visibility: str | None = None,
) -> None:
    async def handler() -> None:
        resolved = await resolve_input(interaction, game, appid)
        if resolved:
            await show_steam(interaction, resolved)

    await run_lookup(interaction, f"/steam game={game!r} appid={appid}", handler, public=public_response(interaction, visibility))


# ---------------------------------------------------------------------------
# /news
# ---------------------------------------------------------------------------
@bot.tree.command(name="news", description="Show recent Steam announcements and news for a game.")
@app_commands.describe(game="Search by game name", appid="Or enter a numeric Steam App ID", visibility="Public or private response")
@app_commands.choices(visibility=VISIBILITY_CHOICES)
@app_commands.autocomplete(game=game_autocomplete)
async def news_command(
    interaction: discord.Interaction,
    game: str | None = None,
    appid: app_commands.Range[int, 1, 999999999] | None = None,
    visibility: str | None = None,
) -> None:
    async def handler() -> None:
        resolved = await resolve_input(interaction, game, appid)
        if not resolved:
            return
        (app, _app_exc), (items, news_exc) = await asyncio.gather(
            capture(services.steam_app(resolved.appid)),
            capture(services.news(resolved.appid, count=max(50, options(interaction)["news_count"] * 10))),
        )
        if news_exc is not None:
            raise news_exc
        name = (app.name if app else None) or resolved.name or services.known_name(resolved.appid) or f"Steam App {resolved.appid}"
        english = [item for item in (items or []) if is_english_news(item)]
        official = [item for item in english if item.feedname == "steam_community_announcements"]
        selected = (official or english)[:options(interaction)["news_count"]]
        await send_embed(interaction, embeds.build_news_embed(name, resolved.appid, selected))

    await run_lookup(interaction, f"/news game={game!r} appid={appid}", handler, public=public_response(interaction, visibility))



def main() -> None:
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set it in your .env file.")
    # log_handler=None: logging is already configured in config.configure_logging()
    bot.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
