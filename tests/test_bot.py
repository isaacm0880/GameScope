"""Current command and persistent component registration smoke tests."""
import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-token-not-real")

import discord
import bot as B


class Registration(unittest.TestCase):
    def test_current_slash_commands(self):
        commands = {command.name: command for command in B.bot.tree.get_commands()}
        self.assertEqual(set(commands), {"game", "settings"})
        self.assertTrue(all(len(command.description) <= 100 for command in commands.values()))

    def test_game_autocomplete_registered(self):
        game = next(command for command in B.bot.tree.get_commands() if command.name == "game")
        param = next(param for param in game._params.values() if param.name == "game")
        self.assertIsNotNone(param.autocomplete)

    def test_persistent_dynamic_component_ids(self):
        for cls, appid, expected in (
            (B.RequirementsButton, 730, "steam:req:730"),
            (B.ReviewsButton, 730, "steam:rev:730"),
            (B.GameViewSelect, 730, "game:view:730"),
            (B.GameNewsButton, 730, "game:news:730"),
        ):
            item = cls(appid)
            self.assertEqual(item.item.custom_id, expected)

    def test_game_view_controls(self):
        steam = B.game_view(730, "Steam", {})
        proton = B.game_view(730, "ProtonDB", {})
        self.assertIsNone(steam.timeout)
        self.assertIsNone(proton.timeout)
        self.assertEqual(sum(isinstance(i, B.GameNewsButton) for i in steam.children), 1)
        self.assertEqual(sum(isinstance(i, B.GameNewsButton) for i in proton.children), 0)
        self.assertTrue(any(isinstance(i, B.GameViewSelect) for i in steam.children))
        self.assertTrue(any(isinstance(i, B.GameViewSelect) for i in proton.children))


if __name__ == "__main__":
    unittest.main()
