"""Persistent guild settings (SQLite)."""
import json
import os
import sqlite3
from pathlib import Path

DEFAULTS = {
    "visibility": "Public", "colour": "#B45D1F", "report_count": 3,
    "news_count": 5, "newest_first": True, "steamdeck": True,
    "anticheat": True, "pcgamingwiki_link": True, "steamdb_link": True,
    "moderator_role": None, "allowed_channels": [],
}

class SettingsStore:
    def __init__(self, path=None):
        self.path = path or os.getenv("SETTINGS_DB", "./data/settings.sqlite3")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (guild INTEGER PRIMARY KEY, value TEXT NOT NULL)")

    def get(self, guild):
        value = dict(DEFAULTS)
        if guild is not None:
            with sqlite3.connect(self.path) as db:
                row = db.execute("SELECT value FROM settings WHERE guild=?", (guild,)).fetchone()
            if row:
                value.update(json.loads(row[0]))
        return value

    def update(self, guild, **changes):
        if guild is None or any(key not in DEFAULTS for key in changes):
            raise ValueError("Invalid settings update")
        value = self.get(guild)
        value.update(changes)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO settings (guild,value) VALUES (?,?) ON CONFLICT(guild) DO UPDATE SET value=excluded.value", (guild, json.dumps(value)))
        return value
