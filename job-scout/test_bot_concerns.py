"""Tests for the bot's 💀 concern threads (Discord faked). Needs discord.py, like test_bot_reactions.py."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import concerns


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "jobs.db")


pytest.importorskip("discord")
import bot  # noqa: E402


def test_skull_is_the_concern_emoji_and_in_help():
    assert bot.normalize_emoji("💀") == bot.CONCERN_EMOJI
    assert "💀 — raise a concern" in bot.HELP_TEXT and "`!concerns`" in bot.HELP_TEXT


def test_only_threads_under_concerns_count(monkeypatch):
    monkeypatch.setattr(bot, "CONCERNS_CHANNEL_ID", 7)
    assert bot.is_concern_thread(SimpleNamespace(parent_id=7))
    assert not bot.is_concern_thread(SimpleNamespace(parent_id=8))
    assert not bot.is_concern_thread(SimpleNamespace(id=7))          # the channel itself
    monkeypatch.setattr(bot, "CONCERNS_CHANNEL_ID", 0)
    assert not bot.is_concern_thread(SimpleNamespace(parent_id=None))


def _message(thread_id, content, attachments=()):
    thread = SimpleNamespace(id=thread_id, send=AsyncMock(), edit=AsyncMock())
    return SimpleNamespace(channel=thread, content=content, attachments=list(attachments), add_reaction=AsyncMock())


def test_messages_in_a_concern_thread_are_saved(monkeypatch, db):
    monkeypatch.setattr(concerns, "DB_PATH", db)
    concerns.open_thread(11, "https://x/1", "SWE Intern", "Acme", "")
    msg = _message(11, "cover letter named the wrong company", [SimpleNamespace(url="https://cdn/shot.png")])
    asyncio.run(bot._handle_concern_message(msg))
    assert concerns.open_concerns()[0]["notes"] == ["cover letter named the wrong company\nhttps://cdn/shot.png"]
    msg.add_reaction.assert_awaited_once_with("📝")


def test_resolve_command_closes_and_archives(monkeypatch, db):
    monkeypatch.setattr(concerns, "DB_PATH", db)
    concerns.open_thread(11, "https://x/1", "SWE Intern", "Acme", "")
    msg = _message(11, "!resolve fixed in a3ff1fa")
    asyncio.run(bot._handle_concern_message(msg))
    assert concerns.open_concerns() == []
    msg.channel.send.assert_awaited_once()
    assert "fixed in a3ff1fa" in msg.channel.send.await_args.args[0]
    msg.channel.edit.assert_awaited_once_with(archived=True)


def test_hand_made_threads_are_ignored(monkeypatch, db):
    monkeypatch.setattr(concerns, "DB_PATH", db)
    msg = _message(55, "random chat")
    asyncio.run(bot._handle_concern_message(msg))
    msg.add_reaction.assert_not_awaited()
    assert concerns.open_concerns() == []
