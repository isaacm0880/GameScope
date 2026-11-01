"""Regression tests for guild isolation, persistence and concurrent writes."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

from settings_store import SettingsStore


def test_guilds_are_independent(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.sqlite3"))
    store.update(123, colour="#112233", report_count=7)
    store.update(456, colour="#ABCDEF")
    assert store.get(123)["colour"] == "#112233"
    assert store.get(123)["report_count"] == 7
    assert store.get(456)["colour"] == "#ABCDEF"
    assert store.get(456)["report_count"] == 3


def test_persists_after_reopen(tmp_path):
    path = str(tmp_path / "settings.sqlite3")
    SettingsStore(path).update(123, visibility="Private")
    assert SettingsStore(path).get(123)["visibility"] == "Private"


def test_global_presence_does_not_change_guild_settings(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.sqlite3"))
    store.update(0, presence_text="Testing")
    assert store.get(0)["presence_text"] == "Testing"
    assert store.get(123)["presence_text"] == "/game"


def test_invalid_settings_rejected(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.sqlite3"))
    try:
        store.update(123, unknown_setting=True)
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown setting was accepted")


def test_simultaneous_updates_preserve_both_fields(tmp_path):
    path = str(tmp_path / "settings.sqlite3")
    store = SettingsStore(path)
    barrier = threading.Barrier(2)

    def write(**kwargs):
        barrier.wait(timeout=5)
        store.update(123, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(write, colour="#123456")
        b = pool.submit(write, report_count=9)
        a.result(timeout=10)
        b.result(timeout=10)

    result = store.get(123)
    assert result["colour"] == "#123456"
    assert result["report_count"] == 9


def test_sqlite_database_has_one_row_per_guild(tmp_path):
    path = str(tmp_path / "settings.sqlite3")
    store = SettingsStore(path)
    store.update(111, colour="#111111")
    store.update(222, colour="#222222")
    with sqlite3.connect(path) as db:
        guild_ids = [row[0] for row in db.execute("SELECT guild FROM settings ORDER BY guild")]
    assert guild_ids == [111, 222]
