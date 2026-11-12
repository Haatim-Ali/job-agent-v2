"""Tests for 💀 concerns: the store in concerns.py (the bot side is in test_bot_concerns.py)."""
import pytest

import concerns


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "jobs.db")


def test_open_note_resolve_cycle(db):
    concerns.open_thread(11, "https://x/1", "SWE Intern", "Acme", "https://discord/link", db_path=db)
    assert concerns.is_tracked(11, db_path=db) and not concerns.is_tracked(12, db_path=db)
    assert concerns.open_thread_for("https://x/1", db_path=db) == 11
    concerns.add_note(11, "location says CA but it's Austin", db_path=db)
    [item] = concerns.open_concerns(db_path=db)
    assert item["title"] == "SWE Intern" and item["notes"] == ["location says CA but it's Austin"]
    assert concerns.resolve(11, "fixed the zone regex", db_path=db)
    assert concerns.open_concerns(db_path=db) == []
    assert concerns.open_thread_for("https://x/1", db_path=db) is None   # a new 💀 opens a fresh thread


def test_typing_in_a_resolved_thread_reopens_it(db):
    concerns.open_thread(11, "https://x/1", "SWE Intern", "Acme", "", db_path=db)
    concerns.resolve(11, db_path=db)
    concerns.add_note(11, "still wrong", db_path=db)
    assert [i["thread_id"] for i in concerns.open_concerns(db_path=db)] == [11]


def test_resolve_unknown_thread_is_false(db):
    assert concerns.resolve(99, db_path=db) is False


def test_thread_name_fits_discord_limit():
    name = concerns.thread_name("R" * 150, "Acme")
    assert name.startswith("💀 RRR") and len(name) == 100


def test_summary_lines_link_each_thread():
    items = [{"thread_id": 5, "title": "SWE", "company": "Acme", "notes": ["a"]},
             {"thread_id": 6, "title": "ML", "company": "Beta", "notes": []}]
    assert concerns.summary_lines(items) == ["• <#5> **SWE** at **Acme** (1 note)",
                                             "• <#6> **ML** at **Beta** (0 notes)"]
