"""Async HTTP access to Steam and ProtonDB.

* One shared ``aiohttp.ClientSession`` for the whole process.
* Per-request timeouts, one retry for transient failures, per-host cool-down
  after HTTP 429 so we never hammer a rate-limited service.
* Results are cached (see ``cache.TTLCache``).
* Methods either return data, return ``None`` for "not found", or raise a
  ``models.UpstreamError`` subclass. Nothing is ever invented.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

import config
from cache import MISSING, TTLCache
from models import (
    BadResponse,
    NewsItem,
    ProtonCounts,
    ProtonSummary,
    RateLimited,
    ReportsResult,
    ResolvedGame,
    Review,
    ReviewSummary,
    SearchResult,
    SteamApp,
    UpstreamUnavailable,
    UpstreamError,
)
from parsers import (
    parse_counts,
    parse_news,
    parse_protondb_reports,
    parse_protondb_summary,
    parse_review_summary,
    parse_reviews,
    parse_search,
    parse_steam_app,
)
from protondb_hash import ordered_variants, report_hash

log = logging.getLogger("protondb-bot.services")

SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails/"
REVIEWS_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
PROTONDB_SUMMARY_URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
PROTONDB_COUNTS_URL = "https://www.protondb.com/data/counts.json"
PROTONDB_REPORTS_URL = "https://www.protondb.com/data/reports/all-devices/app/{hash}.json"
PROTON_PULSE_REPORTS_URL = "https://data.proton-pulse.com/data/{appid}/latest.json"

MAX_CONCURRENT_REQUESTS = 8


def protondb_page_url(appid: int) -> str:
    return f"https://www.protondb.com/app/{appid}"


def steam_reviews_page_url(appid: int) -> str:
    return f"https://steamcommunity.com/app/{appid}/reviews/"


class GameServices:
    def __init__(self, cache: TTLCache | None = None):
        self.cache = cache or TTLCache(config.CACHE_SECONDS, config.CACHE_MAX_ENTRIES)
        self.session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._cooldown_until: dict[str, float] = {}
        self._reports_variant: str | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self.session is not None:
            return
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_SECONDS),
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
            connector=aiohttp.TCPConnector(limit=20),
        )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    # ------------------------------------------------------------------
    # low level
    # ------------------------------------------------------------------
    async def _request_json(
        self,
        url: str,
        *,
        service: str,
        params: dict | None = None,
        timeout: float | None = None,
        retries: int = 1,
    ) -> Any | None:
        """GET ``url`` and decode JSON. Returns None on HTTP 404."""
        if self.session is None:
            raise RuntimeError("GameServices.start() has not been called")
        host = urlsplit(url).netloc
        remaining = self._cooldown_until.get(host, 0.0) - time.monotonic()
        if remaining > 0:
            raise RateLimited(service, remaining)

        req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None
        attempt = 0
        while True:
            try:
                async with self._sem:
                    async with self.session.get(url, params=params, timeout=req_timeout) as resp:
                        status = resp.status
                        if status == 404:
                            return None
                        if status == 429:
                            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                            self._cooldown_until[host] = time.monotonic() + retry_after
                            log.warning("%s rate limited us (HTTP 429) on %s; cooling down %.0fs",
                                        service, host, retry_after)
                            raise RateLimited(service, retry_after)
                        if status == 403:
                            self._cooldown_until[host] = time.monotonic() + 20
                            log.warning("%s refused the request (HTTP 403) on %s", service, host)
                            raise UpstreamUnavailable(service, "HTTP 403")
                        if status >= 500:
                            raise _Transient(f"HTTP {status}")
                        if status >= 400:
                            raise UpstreamUnavailable(service, f"HTTP {status}")
                        raw = await resp.read()
                try:
                    return json.loads(raw)
                except ValueError as exc:
                    raise BadResponse(service, f"invalid JSON from {host}: {exc}") from exc
            except (_Transient, asyncio.TimeoutError, aiohttp.ClientError) as exc:
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.4)
                    continue
                detail = "timed out" if isinstance(exc, asyncio.TimeoutError) else str(exc) or type(exc).__name__
                log.warning("%s request to %s failed: %s", service, host, detail)
                raise UpstreamUnavailable(service, detail) from exc

    async def _cached(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        *,
        negative_ttl: float = config.NEGATIVE_CACHE_SECONDS,
    ) -> Any:
        hit = self.cache.get(key)
        if hit is not MISSING:
            return hit
        value = await loader()
        self.cache.put(key, value, ttl=negative_ttl if value is None else None)
        return value

    # ------------------------------------------------------------------
    # Steam: search / resolve
    # ------------------------------------------------------------------
    async def search_games(self, query: str, *, fast: bool = False) -> list[SearchResult]:
        query = query.strip()
        if len(query) < 2:
            return []
        key = f"search:{config.STEAM_COUNTRY}:{config.STEAM_LANGUAGE}:{query.casefold()}"

        async def load() -> list[SearchResult]:
            payload = await self._request_json(
                SEARCH_URL,
                service="Steam",
                params={"term": query, "l": config.STEAM_LANGUAGE, "cc": config.STEAM_COUNTRY},
                timeout=config.AUTOCOMPLETE_TIMEOUT_SECONDS if fast else None,
                retries=0 if fast else 1,
            )
            results = parse_search(payload, limit=config.CHOICE_LIMIT)
            for r in results:
                self.cache.put(f"name:{r.appid}", r.name)
            return results

        hit = self.cache.get(key)
        if hit is not MISSING:
            return hit
        results = await load()
        self.cache.put(key, results, ttl=None if results else config.NEGATIVE_CACHE_SECONDS)
        return results

    def known_name(self, appid: int) -> str | None:
        name = self.cache.get(f"name:{appid}")
        return None if name is MISSING else name

    async def resolve_game(self, query: str) -> ResolvedGame | None:
        """Turn autocomplete output, a typed App ID or a typed name into an App ID."""
        query = (query or "").strip()
        if not query:
            return None
        if "|" in query:  # older autocomplete values looked like "730|Counter-Strike 2"
            left, right = query.split("|", 1)
            if left.strip().isdigit() and 0 < int(left) <= config.MAX_STEAM_APPID:
                return ResolvedGame(int(left), right.strip() or None)
        if query.isdigit() and 0 < int(query) <= config.MAX_STEAM_APPID:
            appid = int(query)
            return ResolvedGame(appid, self.known_name(appid))
        results = await self.search_games(query)
        if not results:
            return None
        exact = next((r for r in results if r.name.casefold() == query.casefold()), None)
        pick = exact or results[0]
        return ResolvedGame(pick.appid, pick.name)

    # ------------------------------------------------------------------
    # Steam: store data
    # ------------------------------------------------------------------
    async def steam_app(self, appid: int) -> SteamApp | None:
        key = f"app:{config.STEAM_COUNTRY}:{config.STEAM_LANGUAGE}:{appid}"

        async def load() -> SteamApp | None:
            payload = await self._request_json(
                APPDETAILS_URL,
                service="Steam",
                params={"appids": str(appid), "cc": config.STEAM_COUNTRY, "l": config.STEAM_LANGUAGE},
            )
            if payload is None:
                return None
            app = parse_steam_app(appid, payload)
            if app and app.name:
                self.cache.put(f"name:{appid}", app.name)
            return app

        return await self._cached(key, load)

    async def _steam_review_page(self, appid: int, *, recent: bool, limit: int = 5) -> dict:
        """Current Steamworks IUserReviewsService endpoint (legacy /appreviews is deprecated)."""
        params = {
            "input_json": json.dumps({
                "appid": appid,
                "filter": 1 if recent else 0,  # Recent / Helpful
                "languages": [config.STEAM_LANGUAGE if recent else "all"],
                "purchase_type": 1,  # All
                "review_type": 0,  # All
                "num_per_page": max(10, limit * 3) if recent else 1,
                "cursor": "*",
            }, separators=(",", ":")),
        }
        payload = await self._request_json(REVIEWS_URL, service="Steam", params=params)
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
            raise BadResponse("Steam", "GetAppReviews returned no response object")
        return payload["response"]

    async def review_summary(self, appid: int) -> ReviewSummary:
        key = f"review-summary:v2:{appid}"

        async def load() -> ReviewSummary:
            return parse_review_summary(await self._steam_review_page(appid, recent=False))

        return await self._cached(key, load)

    async def recent_reviews(self, appid: int, limit: int = 5) -> list[Review]:
        key = f"recent-reviews:v2:{config.STEAM_LANGUAGE}:{appid}:{limit}"

        async def load() -> list[Review]:
            payload = await self._steam_review_page(appid, recent=True, limit=limit)
            return parse_reviews(payload, limit=limit)

        return await self._cached(key, load)

    async def news(self, appid: int, count: int = 5) -> list[NewsItem]:
        key = f"news:{appid}:{count}"

        async def load() -> list[NewsItem]:
            payload = await self._request_json(
                NEWS_URL,
                service="Steam",
                params={"appid": appid, "count": count, "maxlength": 800, "format": "json"},
            )
            if payload is None:
                return []  # Steam answers 404/403-style for unknown apps
            return parse_news(payload, limit=count)

        hit = self.cache.get(key)
        if hit is not MISSING:
            return hit
        items = await load()
        self.cache.put(key, items, ttl=None if items else config.NEGATIVE_CACHE_SECONDS)
        return items

    # ------------------------------------------------------------------
    # ProtonDB
    # ------------------------------------------------------------------
    async def protondb_summary(self, appid: int) -> ProtonSummary | None:
        key = f"pdb-summary:{appid}"

        async def load() -> ProtonSummary | None:
            payload = await self._request_json(
                PROTONDB_SUMMARY_URL.format(appid=appid), service="ProtonDB"
            )
            if payload is None:
                return None
            return parse_protondb_summary(payload)

        return await self._cached(key, load)

    async def _protondb_counts(self) -> ProtonCounts:
        key = "pdb-counts"
        hit = self.cache.get(key)
        if hit is not MISSING:
            return hit
        payload = await self._request_json(PROTONDB_COUNTS_URL, service="ProtonDB")
        if payload is None:
            raise BadResponse("ProtonDB", "counts.json not found")
        counts = parse_counts(payload)
        self.cache.put(key, counts, ttl=config.COUNTS_CACHE_SECONDS)
        return counts

    async def protondb_reports(self, appid: int, limit: int = 3) -> ReportsResult:
        """Recent individual reports. Never raises; ``ok`` says whether we got real data."""
        key = f"pdb-reports:{appid}:{limit}"
        hit = self.cache.get(key)
        if hit is not MISSING:
            return hit
        try:
            result = await self._load_reports(appid, limit)
        except Exception:  # defensive: reports are a bonus, never break the command
            log.exception("Unexpected error loading ProtonDB reports for app %s", appid)
            result = ReportsResult(ok=False, reason="an unexpected error occurred")
        self.cache.put(key, result, ttl=None if result.ok else config.FAILURE_CACHE_SECONDS)
        return result

    async def _load_reports(self, appid: int, limit: int) -> ReportsResult:
        """Try ProtonDB detailed reports, then the Proton Pulse archived mirror."""
        primary = await self._load_protondb_reports(appid, limit)
        if primary.ok and primary.reports:
            return primary

        try:
            payload = await self._request_json(
                PROTON_PULSE_REPORTS_URL.format(appid=appid),
                service="Proton Pulse",
                retries=0,
            )
            if payload is not None:
                reports = parse_protondb_reports(payload)
                reports.sort(key=lambda r: r.timestamp or 0, reverse=True)
                if reports:
                    log.info("Using Proton Pulse report mirror for Steam app %s", appid)
                    return ReportsResult(reports=reports[:limit], ok=True,
                                         reason="Proton Pulse mirror")
                return primary if not primary.ok else ReportsResult(ok=True, reports=[])
        except (UpstreamError, BadResponse) as exc:
            log.warning("Proton Pulse mirror unavailable for app %s: %s", appid, exc)
        except Exception:
            log.exception("Unexpected Proton Pulse mirror error for app %s", appid)

        return primary

    async def _load_protondb_reports(self, appid: int, limit: int) -> ReportsResult:
        try:
            counts = await self._protondb_counts()
        except UpstreamError as exc:
            log.warning("ProtonDB counts.json unavailable (%s); no individual reports for app %s",
                        exc, appid)
            return ReportsResult(ok=False, reason="ProtonDB's report index is unavailable right now")

        tried: list[str] = []
        for variant in ordered_variants(self._reports_variant):
            digest = report_hash(appid, counts.reports, counts.timestamp, variant)
            url = PROTONDB_REPORTS_URL.format(hash=digest)
            try:
                payload = await self._request_json(url, service="ProtonDB", retries=0)
            except (RateLimited, UpstreamUnavailable) as exc:
                log.warning("ProtonDB report lookup for app %s stopped: %s", appid, exc)
                return ReportsResult(ok=False, reason="ProtonDB is rate limiting or unavailable right now")
            except BadResponse as exc:
                tried.append(f"{variant.name}=non-JSON")
                log.info("ProtonDB variant %s returned non-JSON for app %s: %s", variant.name, appid, exc)
                continue
            if payload is None:
                tried.append(f"{variant.name}=404")
                continue
            try:
                reports = parse_protondb_reports(payload)
            except BadResponse as exc:
                tried.append(f"{variant.name}=unexpected-shape")
                log.warning("ProtonDB variant %s gave an unexpected structure for app %s: %s",
                            variant.name, appid, exc)
                continue
            if self._reports_variant != variant.name:
                log.info("ProtonDB report hash variant %r worked (app %s)", variant.name, appid)
                self._reports_variant = variant.name
            reports.sort(key=lambda r: r.timestamp or 0, reverse=True)
            return ReportsResult(reports=reports[:limit], ok=True)

        log.warning(
            "ProtonDB individual reports unavailable for app %s: no hash variant was accepted "
            "(counts reports=%s timestamp=%s; tried: %s). The endpoint format may have changed.",
            appid, counts.reports, counts.timestamp, ", ".join(tried) or "none",
        )
        return ReportsResult(ok=False, reason="ProtonDB's individual report files could not be located")


class _Transient(Exception):
    """Internal marker for retryable HTTP failures."""


def _parse_retry_after(value: str | None) -> float:
    try:
        seconds = float(value) if value else 30.0
    except ValueError:
        seconds = 30.0
    return min(max(seconds, 5.0), 120.0)
