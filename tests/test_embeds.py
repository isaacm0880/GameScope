"""Regression tests for the current embed layout and Discord limits."""
import unittest

import embeds
from models import Price, ProtonSummary, ReportsResult, ReviewSummary, SteamApp


def text_of(embed):
    return "\n".join([embed.title or "", embed.description or ""] + [
        str(part) for field in embed.fields for part in (field.name, field.value)
    ])


class Embeds(unittest.TestCase):
    def test_steam_embed_fields_and_price(self):
        app = SteamApp(
            appid=10, name="Game",
            price=Price("GBP", 2999, 1499, 50, "£29.99", "£14.99"),
        )
        embed = embeds.build_steam_embed(app, ReviewSummary("Very Positive", 90, 10, 100), False)
        names = [field.name for field in embed.fields]
        for name in ("Steam App ID", "Price", "Release date", "Steam user reviews", "Developer", "Publisher", "Genres"):
            self.assertIn(name, names)
        self.assertEqual(embed.colour.value, 0xB45D1F)
        self.assertEqual(embed.url, app.store_url)
        self.assertIn("90% positive", text_of(embed))
        self.assertIn("£14.99", text_of(embed))

    def test_protondb_embed_uses_url_and_tier(self):
        embed = embeds.build_protondb_embed(
            10, "Game", ProtonSummary("gold", total=5), None,
            ReportsResult(ok=False, reason="unavailable"), None,
        )
        self.assertEqual(embed.url, "https://www.protondb.com/app/10")
        self.assertIn("Gold", text_of(embed))
        self.assertIn("Linux troubleshooting", text_of(embed))

    def test_untrusted_text_does_not_ping_everyone(self):
        app = SteamApp(appid=10, name="@everyone", short_description="<b>@everyone</b>")
        embed = embeds.build_steam_embed(app, None, True)
        self.assertNotIn("@everyone", text_of(embed))

    def test_embed_limits(self):
        app = SteamApp(appid=10, name="X" * 1000, short_description="Y" * 10000)
        embed = embeds.build_steam_embed(app, None, True)
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.title), 256)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    def test_requirements_missing(self):
        embed = embeds.build_requirements_embed(SteamApp(appid=10, name="Game"))
        self.assertIn("doesn't list", embed.description)

    def test_reviews_link(self):
        embed = embeds.build_reviews_embed("Game", 10, None, True, [], False)
        self.assertEqual(embed.url, "https://steamcommunity.com/app/10/reviews/")
        self.assertIn("All reviews", [field.name for field in embed.fields])


if __name__ == "__main__":
    unittest.main()
