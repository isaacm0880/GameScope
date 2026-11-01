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


def steam_buttons_view(appid: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RequirementsButton(appid))
    view.add_item(ReviewsButton(appid))
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
        services.protondb_reports(appid),
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
    await send_embed(interaction, embed)


@bot.tree.command(name="protondb", description="Check a game's Linux compatibility on ProtonDB.")
@app_commands.describe(game="Search by game name", appid="Or enter a numeric Steam App ID")
@app_commands.autocomplete(game=game_autocomplete)
async def protondb_command(
    interaction: discord.Interaction,
    game: str | None = None,
    appid: app_commands.Range[int, 1, 999999999] | None = None,
) -> None:
    async def handler() -> None:
        resolved = await resolve_input(interaction, game, appid)
        if resolved:
            await show_protondb(interaction, resolved)

    await run_lookup(interaction, f"/protondb game={game!r} appid={appid}", handler, public=True)


# ---------------------------------------------------------------------------
# /steam
# ---------------------------------------------------------------------------
async def show_steam(interaction: discord.Interaction, resolved: ResolvedGame) -> None:
    (app, app_exc), (summary, summary_exc) = await asyncio.gather(
        capture(services.steam_app(resolved.appid)),
        capture(services.review_summary(resolved.appid)),
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
    await send_embed(interaction, embed, view=steam_buttons_view(app.appid))


@bot.tree.command(name="steam", description="Show Steam store info: price, reviews, release date and more.")
@app_commands.describe(game="Search by game name", appid="Or enter a numeric Steam App ID")
@app_commands.autocomplete(game=game_autocomplete)
async def steam_command(
    interaction: discord.Interaction,
    game: str | None = None,
    appid: app_commands.Range[int, 1, 999999999] | None = None,
) -> None:
    async def handler() -> None:
        resolved = await resolve_input(interaction, game, appid)
        if resolved:
            await show_steam(interaction, resolved)

    await run_lookup(interaction, f"/steam game={game!r} appid={appid}", handler, public=True)


# ---------------------------------------------------------------------------
# /news
# ---------------------------------------------------------------------------
@bot.tree.command(name="news", description="Show recent Steam announcements and news for a game.")
@app_commands.describe(game="Search by game name", appid="Or enter a numeric Steam App ID")
@app_commands.autocomplete(game=game_autocomplete)
async def news_command(
    interaction: discord.Interaction,
    game: str | None = None,
    appid: app_commands.Range[int, 1, 999999999] | None = None,
) -> None:
    async def handler() -> None:
        resolved = await resolve_input(interaction, game, appid)
        if not resolved:
            return
        (app, _app_exc), (items, news_exc) = await asyncio.gather(
            capture(services.steam_app(resolved.appid)),
            capture(services.news(resolved.appid)),
        )
        if news_exc is not None:
            raise news_exc
        name = (app.name if app else None) or resolved.name or services.known_name(resolved.appid) or f"Steam App {resolved.appid}"
        await send_embed(interaction, embeds.build_news_embed(name, resolved.appid, items or []))

    await run_lookup(interaction, f"/news game={game!r} appid={appid}", handler, public=True)


def main() -> None:
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set it in your .env file.")
    # log_handler=None: logging is already configured in config.configure_logging()
    bot.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
