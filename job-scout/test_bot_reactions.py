"""
Tests for the Discord reaction mapping (👍 tailor, ✅ applied, ❌ dismiss).
Run with: python -m pytest job-scout/test_bot_reactions.py -v  (needs discord.py installed)
"""

import pytest

pytest.importorskip("discord")
import bot  # noqa: E402


@pytest.mark.parametrize("raw", ["👍", "👍🏻", "👍🏽", "👍🏿", "👍️"])
def test_thumbs_up_any_skin_tone_is_tailor(raw):
    assert bot.normalize_emoji(raw) == bot.TAILOR_EMOJI == "👍"


def test_check_mark_is_applied():
    assert bot.normalize_emoji("✅") in bot.APPLIED_EMOJI_ALIASES
    assert bot.APPLIED_EMOJI == "✅"


def test_old_envelope_still_marks_applied():
    assert bot.normalize_emoji("📨") in bot.APPLIED_EMOJI_ALIASES


def test_check_mark_no_longer_tailors():
    assert bot.normalize_emoji("✅") != bot.TAILOR_EMOJI


def test_help_text_matches_mapping():
    assert "👍 — generate tailored resume" in bot.HELP_TEXT
    assert "✅ — mark as applied" in bot.HELP_TEXT


def test_check_mark_works_on_tailored_files_message():
    content = ("📎 **Tailored resume + cover letter:** Software Engineer Intern @ Glean\n"
               "<https://job-boards.greenhouse.io/gleanwork/jobs/4595665005>")
    assert bot.parse_tailored_message(content) == (
        "https://job-boards.greenhouse.io/gleanwork/jobs/4595665005", "Software Engineer Intern", "Glean")
    assert bot.parse_tailored_message("GAPS TO PREPARE FOR:") is None


def test_reactions_in_job_alerts_are_answered_in_the_actions_channel(monkeypatch):
    # #job-alerts stays alerts-only (2026-10-26)
    monkeypatch.setattr(bot, "CHANNEL_ID", 1)
    monkeypatch.setattr(bot, "REQUESTS_CHANNEL_ID", 2)
    monkeypatch.setattr(bot, "ACTIONS_CHANNEL_ID", 3)
    assert bot.reply_channel_id(1) == 3
    # ✅ on a tailored-files message in #requests answers in #reactions too (2026-10-26)
    assert bot.reply_channel_id(2) == 3 and bot.reply_channel_id(3) == 3
    assert bot.bot_channels() == {1, 2, 3}


def test_without_an_actions_channel_replies_stay_put(monkeypatch):
    monkeypatch.setattr(bot, "CHANNEL_ID", 1)
    monkeypatch.setattr(bot, "ACTIONS_CHANNEL_ID", 0)
    assert bot.reply_channel_id(1) == 1
    assert 0 not in bot.bot_channels()


def test_alerts_are_not_auto_tailored_by_default():
    import json, os
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "scraper_config.json")))
    assert cfg["tailor_every_alert"] is False
