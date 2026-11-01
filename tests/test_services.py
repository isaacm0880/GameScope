import json
import asyncio, json, unittest
import stubs; stubs.install()
import aiohttp
import config
from cache import TTLCache
from models import BadResponse, RateLimited, UpstreamUnavailable
from protondb_hash import VARIANTS, report_hash
import services as S


class Resp:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    async def read(self):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """route: callable(url, params) -> Resp | Exception"""
    def __init__(self, route):
        self.route, self.calls = route, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        r = self.route(url, params)
        if isinstance(r, Exception):
            raise r
        return r


def J(obj, status=200):
    return Resp(status, json.dumps(obj).encode())


def make(route):
    svc = S.GameServices(TTLCache(900, 100))
    svc.session = FakeSession(route)
    return svc


def run(coro):
    return asyncio.run(coro)


class Http(unittest.TestCase):
    def test_404_returns_none_and_bad_json(self):
        svc = make(lambda u, p: Resp(404))
        self.assertIsNone(run(svc._request_json("https://a.b/x", service="T")))
        svc = make(lambda u, p: Resp(200, b"<html>cloudflare</html>"))
        with self.assertRaises(BadResponse):
            run(svc._request_json("https://a.b/x", service="T"))

    def test_429_sets_cooldown_and_blocks_next_call(self):
        svc = make(lambda u, p: Resp(429, headers={"Retry-After": "40"}))
        with self.assertRaises(RateLimited):
            run(svc._request_json("https://a.b/x", service="T"))
        n = len(svc.session.calls)
        with self.assertRaises(RateLimited):
            run(svc._request_json("https://a.b/y", service="T"))
        self.assertEqual(len(svc.session.calls), n)  # no new network call

    def test_5xx_retries_once_then_unavailable(self):
        svc = make(lambda u, p: Resp(503))
        with self.assertRaises(UpstreamUnavailable):
            run(svc._request_json("https://a.b/x", service="T"))
        self.assertEqual(len(svc.session.calls), 2)

    def test_timeout_and_client_error(self):
        for exc in (asyncio.TimeoutError(), aiohttp.ClientError("boom")):
            svc = make(lambda u, p, e=exc: e)
            with self.assertRaises(UpstreamUnavailable):
                run(svc._request_json("https://a.b/x", service="T", retries=0))


class Steam(unittest.TestCase):
    def test_search_uses_cc_gb_and_caches(self):
        svc = make(lambda u, p: J({"items": [{"type": "app", "id": 1, "name": "Half-Life"}]}))
        r1 = run(svc.search_games("half"))
        r2 = run(svc.search_games("HALF"))
        self.assertEqual(r1[0].appid, 1)
        self.assertEqual(len(svc.session.calls), 1)
        self.assertEqual(svc.session.calls[0][1]["cc"], "gb")
        self.assertEqual(svc.known_name(1), "Half-Life")

    def test_resolve_variants(self):
        svc = make(lambda u, p: J({"items": [{"type": "app", "id": 7, "name": "Foo"},
                                              {"type": "app", "id": 8, "name": "Foo 2"}]}))
        self.assertEqual(run(svc.resolve_game("730")).appid, 730)
        self.assertEqual(run(svc.resolve_game("730|Counter-Strike 2")).name, "Counter-Strike 2")
        self.assertEqual(run(svc.resolve_game("foo 2")).appid, 8)
        self.assertEqual(run(svc.resolve_game("zzz")).appid, 7)  # first hit
        self.assertIsNone(run(make(lambda u, p: J({"items": []})).resolve_game("nothing")))
        self.assertIsNone(run(svc.resolve_game("  ")))

    def test_app_review_news_requests(self):
        def route(u, p):
            if "appdetails" in u:
                return J({"5": {"success": True, "data": {"name": "G"}}})
            if "GetAppReviews" in u:
                return J({"response": {"query_summary": {"total_positive": 1, "total_negative": 1, "total_reviews": 2},
                          "reviews": [{"review": "ok", "voted_up": True}]}})
            if "GetNewsForApp" in u:
                return J({"appnews": {"newsitems": [{"title": "N", "date": 5}]}})
        svc = make(route)
        self.assertEqual(run(svc.steam_app(5)).name, "G")
        self.assertEqual(run(svc.review_summary(5)).total_reviews, 2)
        self.assertEqual(len(run(svc.recent_reviews(5))), 1)
        self.assertEqual(run(svc.news(5))[0].title, "N")
        app_call = [c for c in svc.session.calls if "appdetails" in c[0]][0]
        self.assertEqual(app_call[1]["cc"], "gb"); self.assertEqual(app_call[1]["l"], "english")
        rev_calls = [c[1] for c in svc.session.calls if "GetAppReviews" in c[0]]
        self.assertEqual(json.loads(rev_calls[0]["input_json"])["languages"], ["all"])  # overall rating across languages

    def test_missing_app_cached_as_none(self):
        svc = make(lambda u, p: J({"9": {"success": False}}))
        self.assertIsNone(run(svc.steam_app(9)))
        self.assertIsNone(run(svc.steam_app(9)))
        self.assertEqual(len(svc.session.calls), 1)

    def test_news_404_is_empty(self):
        self.assertEqual(run(make(lambda u, p: Resp(404)).news(1)), [])


class Reports(unittest.TestCase):
    COUNTS = {"reports": 412345, "timestamp": 1730000000}
    PAGE = {"reports": [{"timestamp": 100, "responses": {"protonVersion": "8", "verdict": "no"}},
                        {"timestamp": 300, "responses": {"protonVersion": "9", "verdict": "yes"}}]}

    def route_for(self, good_variant):
        good = report_hash(77, 412345, 1730000000, good_variant)

        def route(u, p):
            if u.endswith("counts.json"):
                return J(self.COUNTS)
            if u.endswith(f"/{good}.json"):
                return J(self.PAGE)
            return Resp(404)
        return route

    def test_finds_working_variant_remembers_and_sorts(self):
        for v in VARIANTS:
            svc = make(self.route_for(v))
            res = run(svc.protondb_reports(77))
            self.assertTrue(res.ok, v.name)
            self.assertEqual([r.timestamp for r in res.reports], [300, 100])
            self.assertEqual(svc._reports_variant, v.name)

    def test_all_404_is_not_ok_and_never_fabricates(self):
        svc = make(lambda u, p: J(self.COUNTS) if u.endswith("counts.json") else Resp(404))
        res = run(svc.protondb_reports(77))
        self.assertFalse(res.ok); self.assertEqual(res.reports, []); self.assertTrue(res.reason)
        calls = len(svc.session.calls)
        run(svc.protondb_reports(77))
        self.assertEqual(len(svc.session.calls), calls)  # failure cached briefly

    def test_html_counts_and_rate_limit_degrade(self):
        svc = make(lambda u, p: Resp(200, b"<html>"))
        self.assertFalse(run(svc.protondb_reports(1)).ok)
        svc = make(lambda u, p: J(self.COUNTS) if u.endswith("counts.json") else Resp(429))
        res = run(svc.protondb_reports(1))
        self.assertFalse(res.ok)
        self.assertEqual(len(svc.session.calls), 3)  # counts, rate-limited primary, then fallback mirror

    def test_wrong_shape_skipped(self):
        svc = make(lambda u, p: J(self.COUNTS) if u.endswith("counts.json") else J({"hello": 1}))
        self.assertFalse(run(svc.protondb_reports(1)).ok)


if __name__ == "__main__":
    unittest.main()
