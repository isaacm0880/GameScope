"""Persistent guild settings (SQLite)."""
import json
import os
import sqlite3
from pathlib import Path

DEFAULTS = {
    "visibility": "Public", "colour": "#B45D1F", "report_count": 3,
    "news_count": 5, "pcgamingwiki_link": True, "steamdb_link": True,
    "settings_roles": [], "allowed_channels": [],
    "presence_status": "online", "presence_type": "watching", "presence_text": "/game", "presence_enabled": True,
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
                value.update({k: v for k, v in json.loads(row[0]).items() if k in DEFAULTS})
        return value

    def update(self, guild, **changes):
        if guild is None or any(key not in DEFAULTS for key in changes):
            raise ValueError("Invalid settings update")
        # Lock before reading so concurrent updates cannot overwrite each other.
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE guild=?", (guild,)).fetchone()
            value = dict(DEFAULTS)
            if row:
                value.update(json.loads(row[0]))
            value.update(changes)
            db.execute(
                "INSERT INTO settings (guild,value) VALUES (?,?) "
                "ON CONFLICT(guild) DO UPDATE SET value=excluded.value",
                (guild, json.dumps(value)),
            )
        return value
