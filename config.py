"""Configuration and shared constants.

Every setting is read from environment variables (normally via the existing
``.env`` file). Existing variables keep working; new ones are optional.
"""
from __future__ import annotations

import logging
import os

import discord
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Shared embed colour (#B45D1F). Defined once; every embed is created through
# ``embeds.new_embed`` so commands, buttons and error embeds all use it.
# ---------------------------------------------------------------------------
EMBED_COLOUR = discord.Colour(0xB45D1F)


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logging.getLogger("gamescope").warning(
            "Ignoring invalid %s=%r; using %s", name, raw, default
        )
        return default


def _float_env(name: str, default: float, minimum: float = 1.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logging.getLogger("gamescope").warning(
            "Ignoring invalid %s=%r; using %s", name, raw, default
        )
        return default


# --- Existing settings (unchanged names) -----------------------------------
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
CACHE_SECONDS = _int_env("CACHE_SECONDS", 900)
LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

# --- New, optional settings ------------------------------------------------
# Steam store region / language. Defaults: UK store, English.
_cc = os.getenv("STEAM_COUNTRY", "gb").strip().lower()
STEAM_COUNTRY = _cc if len(_cc) == 2 and _cc.isalpha() else "gb"
STEAM_LANGUAGE = os.getenv("STEAM_LANGUAGE", "english").strip().lower() or "english"
HTTP_TIMEOUT_SECONDS = _float_env("HTTP_TIMEOUT_SECONDS", 12.0)

# Autocomplete must answer within Discord's 3 second limit.
AUTOCOMPLETE_TIMEOUT_SECONDS = 2.5

USER_AGENT = "GameScope/1.0 (Discord bot; community compatibility lookup)"

# Cache tuning (not user-facing)
CACHE_MAX_ENTRIES = 1000
NEGATIVE_CACHE_SECONDS = 60       # "not found" answers
FAILURE_CACHE_SECONDS = 120       # ProtonDB report lookups that failed
COUNTS_CACHE_SECONDS = 300        # ProtonDB counts.json token

# Discord limits
CHOICE_LIMIT = 25
CHOICE_NAME_MAX = 100
MAX_STEAM_APPID = 2_147_483_647


def configure_logging() -> logging.Logger:
    level = logging.getLevelName(LOG_LEVEL_NAME)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return logging.getLogger("gamescope")
