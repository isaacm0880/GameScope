"""Discord embed builders.

Every embed is created through :func:`new_embed`, which applies the shared
colour and is the only place embeds are constructed, so success, button and
error embeds all look the same. All text that originates outside the bot is
cleaned (HTML/BBCode removed, mentions neutralised, markdown escaped) and cut to
Discord's limits before it is attached.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord
from discord.utils import escape_markdown

import config
from models import (
    NewsItem,
    ProtonReport,
    ProtonSummary,
    ReportsResult,
    Review,
    ReviewSummary,
    SteamApp,
)
from services import protondb_page_url, steam_reviews_page_url
from textutil import (
    compact_lines,
    html_to_text,
    neutralise,
    news_preview,
    safe_http_url,
    truncate,
)

# Discord limits
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FOOTER_MAX = 2048
MAX_FIELDS = 25
TOTAL_MAX = 6000
TOTAL_BUDGET = 5800  # leave headroom

TIER_INFO = {
    "platinum": ("Platinum", "💎", "Runs perfectly or very well with no significant tweaks."),
    "gold": ("Gold", "🟡", "Runs well after some tweaks or workarounds."),
    "silver": ("Silver", "⚪", "Runs with issues; may need additional tweaks."),
    "bronze": ("Bronze", "🟤", "Runs, but significant issues may affect play."),
    "borked": ("Borked", "🔴", "Reports indicate it is unplayable or severely broken."),
    "pending": ("Pending", "⏳", "Not enough community reports yet to assign a tier."),
}


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def new_embed(**kwargs: Any) -> discord.Embed:
    """Create an embed with the shared colour."""
    kwargs["colour"] = config.EMBED_COLOUR
    return discord.Embed(**kwargs)


def safe_text(value: object) -> str:
    """Escape untrusted text for display (mentions, masked links, markdown)."""
    return escape_markdown(neutralise(str(value)))


def title_text(value: object, limit: int = TITLE_MAX) -> str:
    """Titles and field names: mentions/links neutralised, no markdown escaping
    (a stray backslash would be shown literally there)."""
    return neutralise(truncate(str(value), limit))


def add_field(embed: discord.Embed, name: str, value: str | None, inline: bool = False) -> None:
    value = (value or "").strip() or "—"
    embed.add_field(
        name=truncate(name, FIELD_NAME_MAX),
        value=truncate(value, FIELD_VALUE_MAX),
        inline=inline,
    )


def enforce_limits(embed: discord.Embed) -> discord.Embed:
    """Final safety net so sending can never fail on embed size limits."""
    if embed.title:
        embed.title = truncate(embed.title, TITLE_MAX)
    if embed.description:
        embed.description = truncate(embed.description, DESCRIPTION_MAX)
    if embed.footer and embed.footer.text:
        embed.set_footer(text=truncate(embed.footer.text, FOOTER_MAX))
    while len(embed.fields) > MAX_FIELDS:
        embed.remove_field(len(embed.fields) - 1)
    for i, f in enumerate(embed.fields):
        embed.set_field_at(
            i,
            name=truncate(f.name, FIELD_NAME_MAX) or "—",
            value=truncate(f.value, FIELD_VALUE_MAX) or "—",
            inline=f.inline,
        )

    for _ in range(50):
        if len(embed) <= TOTAL_BUDGET:
            break
        # shrink whichever text is currently the longest
        candidates: list[tuple[int, str, int | None]] = []
        if embed.description:
            candidates.append((len(embed.description), "description", None))
        for i, f in enumerate(embed.fields):
            candidates.append((len(f.value or ""), "field", i))
        if not candidates:
            break
        length, kind, idx = max(candidates)
        if length <= 40:
            break
        new_len = max(40, int(length * 0.7))
        if kind == "description":
            embed.description = truncate(embed.description, new_len)
        else:
            f = embed.fields[idx]
            embed.set_field_at(idx, name=f.name, value=truncate(f.value, new_len), inline=f.inline)
    while len(embed) > TOTAL_MAX and embed.fields:  # last resort: drop trailing fields
        embed.remove_field(len(embed.fields) - 1)
    return embed


def _fmt_int(n: int | None) -> str:
    return f"{n:,}" if isinstance(n, int) else "?"


def _date(ts: int | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# generic notices
# --------------------------------------------------------------------------
def error_embed(message: str, title: str = "Something went wrong") -> discord.Embed:
    return enforce_limits(new_embed(title=title, description=truncate(message, 1500)))


# --------------------------------------------------------------------------
# /protondb
# --------------------------------------------------------------------------
TROUBLESHOOTING = (
    "• Read recent reports from people with a similar GPU, driver and distro.\n"
    "• Update GPU drivers (Mesa / NVIDIA) and make sure Vulkan works.\n"
    "• Try a different Proton: latest stable, Proton Experimental, or GE-Proton "
    "(Steam → Properties → Compatibility).\n"
    "• Clear launch options, then add tweaks one at a time. "
    "`PROTON_LOG=1 %command%` writes `~/steam-{appid}.log` for diagnosis.\n"
    "• Check the game's anti-cheat and launcher support: areweanticheatyet.com"
)


def _tier_line(tier: str | None) -> str:
    if not tier:
        return "Unknown"
    info = TIER_INFO.get(tier.lower())
    if info:
        return f"{info[1]} **{info[0]}**"
    return f"**{safe_text(tier.title())}**"


def _report_field(report: ProtonReport) -> tuple[str, str]:
    if report.verdict == "yes":
        mark = "👍"
    elif report.verdict == "no":
        mark = "👎"
    else:
        mark = "•"
    bits = [b for b in (_date(report.timestamp), report.proton_version and f"Proton {report.proton_version}") if b]
    if report.rating:
        bits.append(report.rating.title())
    name = f"{mark} " + (" · ".join(bits) if bits else "Report")
    lines = []
    if report.hardware:
        lines.append(f"*{safe_text(truncate(report.hardware, 160))}*")
    if report.notes:
        lines.append(safe_text(truncate(report.notes.replace("\n", " "), 300)))
    return title_text(name, FIELD_NAME_MAX), "\n".join(lines) or "No written notes."


def build_protondb_embed(
    appid: int,
    name: str,
    summary: ProtonSummary | None,
    summary_error: str | None,
    reports: ReportsResult,
    app: SteamApp | None,
) -> discord.Embed:
    page = protondb_page_url(appid)
    embed = new_embed(
        title=title_text(name),
        url=page,
        description=f"Steam App ID: `{appid}` · Linux / Proton compatibility",
        timestamp=datetime.now(timezone.utc),
    )
    image = safe_http_url(app.header_image) if app else None
    if image:
        embed.set_thumbnail(url=image)

    # tier + explanation
    if summary_error:
        tier_text = f"ProtonDB summary is unavailable right now ({safe_text(summary_error)})."
    elif summary is None:
        tier_text = "ProtonDB has no summary for this game yet."
    elif summary.tier and summary.tier in TIER_INFO:
        label, emoji, explanation = TIER_INFO[summary.tier]
        tier_text = f"{emoji} **{label}**\n{explanation}"
    elif summary.tier:
        tier_text = f"**{safe_text(summary.tier.title())}**"
    else:
        tier_text = "ProtonDB returned data, but no recognised tier."
    add_field(embed, "ProtonDB tier", tier_text, inline=True)

    # statistics
    if summary:
        stats = []
        if summary.total is not None:
            stats.append(f"Reports: **{_fmt_int(summary.total)}**")
        if summary.confidence:
            stats.append(f"Confidence: **{safe_text(summary.confidence.title())}**")
        if summary.trending_tier and summary.trending_tier in TIER_INFO:
            stats.append(f"Trending: {_tier_line(summary.trending_tier)}")
        if summary.best_reported_tier and summary.best_reported_tier in TIER_INFO:
            stats.append(f"Best reported: {_tier_line(summary.best_reported_tier)}")
        if summary.score is not None:
            stats.append(f"Score: **{summary.score:.2f}**")
        add_field(embed, "Community data", "\n".join(stats) or "No statistics returned.", inline=True)

    # individual reports
    if reports.ok and reports.reports:
        for report in reports.reports:
            rname, rvalue = _report_field(report)
            add_field(embed, rname, rvalue, inline=False)
    elif reports.ok:
        add_field(embed, "Recent community reports",
                  f"ProtonDB returned no individual reports for this game. [View the page]({page})")
    else:
        why = f" ({safe_text(reports.reason)})" if reports.reason else ""
        add_field(
            embed,
            "Recent community reports",
            f"Individual reports couldn't be retrieved right now{why}. "
            f"Read them directly on [ProtonDB]({page}).",
        )

    add_field(embed, "Linux troubleshooting", TROUBLESHOOTING.replace("{appid}", str(appid)))
    embed.set_footer(text="Source: ProtonDB community reports. Compatibility can change with game or Proton updates.")
    return enforce_limits(embed)


# --------------------------------------------------------------------------
# /steam
# --------------------------------------------------------------------------
def _raw_price(cents: int | None, currency: str | None) -> str:
    if cents is None:
        return "?"
    return f"{cents / 100:.2f} {currency or ''}".strip()


def price_text(app: SteamApp) -> str:
    if app.is_free:
        return "**Free to Play**"
    price = app.price
    if price and (price.final_formatted or price.final is not None):
        final = price.final_formatted or _raw_price(price.final, price.currency)
        if price.discount_percent > 0:
            initial = price.initial_formatted or _raw_price(price.initial, price.currency)
            return f"~~{neutralise(initial)}~~ **{neutralise(final)}** (-{price.discount_percent}%)"
        return f"**{neutralise(final)}**"
    if app.coming_soon:
        return "Not yet released"
    return "Price unavailable"


def review_summary_text(summary: ReviewSummary | None, unavailable: bool = False) -> str:
    if unavailable or summary is None:
        return "Currently unavailable from Steam."
    if summary.total_reviews <= 0 and summary.total_positive + summary.total_negative <= 0:
        return "No user reviews yet."
    desc = summary.review_score_desc or "Reviews"
    pct = summary.percent_positive
    head = f"**{safe_text(desc)}**" + (f" — {pct}% positive" if pct is not None else "")
    return (
        f"{head}\n👍 {_fmt_int(summary.total_positive)} · 👎 {_fmt_int(summary.total_negative)} "
        f"({_fmt_int(summary.total_reviews)} reviews)"
    )


def build_steam_embed(app: SteamApp, summary: ReviewSummary | None, reviews_unavailable: bool) -> discord.Embed:
    name = app.name or f"Steam App {app.appid}"
    description = ""
    if app.short_description:
        description = safe_text(truncate(html_to_text(app.short_description, keep_lines=False), 600))
    embed = new_embed(title=title_text(name), url=app.store_url, description=description or None)
    image = safe_http_url(app.header_image)
    if image:
        embed.set_image(url=image)

    id_text = f"`{app.appid}`"
    if app.app_type and app.app_type.lower() != "game":
        id_text += f" ({safe_text(app.app_type)})"
    add_field(embed, "Steam App ID", id_text, inline=True)
    add_field(embed, "Price", price_text(app), inline=True)
    release = app.release_date or ("Coming soon" if app.coming_soon else None)
    add_field(embed, "Release date", safe_text(release) if release else "Not available", inline=True)
    add_field(embed, "Steam user reviews", review_summary_text(summary, reviews_unavailable), inline=False)
    add_field(embed, "Developer",
              safe_text(", ".join(app.developers)) if app.developers else "Not available", inline=True)
    add_field(embed, "Publisher",
              safe_text(", ".join(app.publishers)) if app.publishers else "Not available", inline=True)
    add_field(embed, "Genres",
              safe_text(", ".join(app.genres)) if app.genres else "Not available", inline=False)
    embed.set_footer(text=f"Steam store data ({config.STEAM_COUNTRY.upper()} region). Prices and reviews change over time.")
    return enforce_limits(embed)


# --------------------------------------------------------------------------
# Requirements button
# --------------------------------------------------------------------------
def _requirements_text(raw: str | None) -> str | None:
    if not raw:
        return None
    text = compact_lines(html_to_text(raw))
    lines = text.split("\n")
    # drop Steam's own "Minimum:" / "Recommended:" header line; the field name says it
    if lines and lines[0].rstrip(":").strip().lower() in ("minimum", "recommended"):
        lines = lines[1:]
    text = "\n".join(lines).strip()
    return safe_text(text) if text else None


def build_requirements_embed(app: SteamApp) -> discord.Embed:
    name = app.name or f"Steam App {app.appid}"
    embed = new_embed(
        title=title_text(f"{name} — PC system requirements"),
        url=app.store_url,
    )
    minimum = _requirements_text(app.requirements_min)
    recommended = _requirements_text(app.requirements_rec)
    if not minimum and not recommended:
        embed.description = "Steam doesn't list PC system requirements for this game."
    else:
        add_field(embed, "Minimum", minimum or "Not published.", inline=False)
        add_field(embed, "Recommended", recommended or "Not published.", inline=False)
    embed.set_footer(text="PC requirements as published on the Steam store page.")
    return enforce_limits(embed)


# --------------------------------------------------------------------------
# Reviews button
# --------------------------------------------------------------------------
def _review_field(review: Review) -> tuple[str, str]:
    if review.voted_up is True:
        mark = "👍 Recommended"
    elif review.voted_up is False:
        mark = "👎 Not recommended"
    else:
        mark = "Review"
    bits = [mark]
    if review.playtime_minutes:
        bits.append(f"{review.playtime_minutes / 60:.1f} h on record")
    date = _date(review.timestamp)
    if date:
        bits.append(date)
    return " · ".join(bits), safe_text(truncate(review.text, 350))


def build_reviews_embed(
    app_name: str,
    appid: int,
    summary: ReviewSummary | None,
    summary_unavailable: bool,
    reviews: list[Review] | None,
    reviews_unavailable: bool,
) -> discord.Embed:
    embed = new_embed(
        title=title_text(f"{app_name} — Steam reviews"),
        url=steam_reviews_page_url(appid),
    )
    add_field(embed, "Overall rating", review_summary_text(summary, summary_unavailable), inline=False)
    if reviews_unavailable:
        add_field(embed, "Recent reviews", "Recent reviews are currently unavailable from Steam.", inline=False)
    elif not reviews:
        add_field(embed, "Recent reviews", f"No recent {config.STEAM_LANGUAGE.title()} reviews with text.", inline=False)
    else:
        for review in reviews:
            fname, fvalue = _review_field(review)
            add_field(embed, fname, fvalue, inline=False)
    add_field(embed, "All reviews", f"[Read all reviews on Steam]({steam_reviews_page_url(appid)})", inline=False)
    embed.set_footer(text=f"Overall rating covers all languages; recent reviews are {config.STEAM_LANGUAGE.title()} only.")
    return enforce_limits(embed)


# --------------------------------------------------------------------------
# /news
# --------------------------------------------------------------------------
def news_link(item: NewsItem, appid: int) -> str | None:
    """Direct link to the announcement."""
    if item.gid and (item.feedname == "steam_community_announcements"
                     or (item.url and "steam_community_announcements" in item.url)):
        return f"https://store.steampowered.com/news/app/{appid}/view/{item.gid}"
    return safe_http_url(item.url)


def build_news_embed(name: str, appid: int, items: list[NewsItem]) -> discord.Embed:
    embed = new_embed(
        title=title_text(f"{name} — recent Steam news"),
        url=f"https://store.steampowered.com/news/app/{appid}",
    )
    if not items:
        embed.description = f"No recent announcements or news were found on Steam for **{safe_text(name)}**."
        return enforce_limits(embed)
    for item in items[:5]:
        bits = []
        if item.date:
            bits.append(f"<t:{item.date}:D>")
        if item.feedlabel:
            bits.append(safe_text(item.feedlabel))
        header = " · ".join(bits)
        preview = safe_text(news_preview(item.contents)) or "No text preview available."
        link = news_link(item, appid)
        value = (f"{header}\n" if header else "") + preview
        if link:
            value += f"\n[Read the announcement]({link})"
        add_field(embed, title_text(item.title, FIELD_NAME_MAX), value, inline=False)
    embed.set_footer(text="Announcements and news from Steam.")
    return enforce_limits(embed)
