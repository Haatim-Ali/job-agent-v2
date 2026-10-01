"""
Tests for claude_json.py: the single helper every Claude call goes through.
Run with: python -m pytest job-scout/test_claude_json.py -v
"""

import pytest

import claude_json as cj


class Fake:
    def __init__(self, text='{"ok": true}', stop="end_turn"):
        self.sent, self.text, self.stop = {}, text, stop
        self.messages = self

    def create(self, **kw):
        self.sent = kw
        text, stop = self.text, self.stop

        class R:
            stop_reason = stop
            content = [type("T", (), {"type": "thinking"})(), type("B", (), {"type": "text", "text": text})()]
            usage = type("U", (), {"cache_read_input_tokens": 0})()
        return R()


def test_obj_requires_everything_and_forbids_extras():
    assert cj.obj({"a": cj.STR, "b": cj.INT}) == {
        "type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a", "b"], "additionalProperties": False}


def test_ask_json_sends_schema_and_parses_text_block():
    client = Fake()
    schema = cj.obj({"ok": cj.BOOL})
    assert cj.ask_json(client, "hi", schema, system=cj.cached_system("kb")) == {"ok": True}
    assert client.sent["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert client.sent["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_unusable_replies_raise(stop):
    with pytest.raises(cj.ClaudeJSONError):
        cj.ask_json(Fake(stop=stop), "hi", cj.obj({"ok": cj.BOOL}))


def test_temperature_only_for_models_that_accept_it():
    assert cj.sampling("claude-sonnet-4-6", 0.3) == {"temperature": 0.3}
    assert cj.sampling("claude-haiku-4-5-20251001", 0) == {"temperature": 0}
    assert cj.sampling("claude-sonnet-5-5", 0.3) == {}
    assert cj.sampling("claude-sonnet-4-6", None) == {}


def test_kb_text_is_stable():
    kb = {"b": 1, "a": {"y": 2, "x": "é"}}
    assert cj.kb_system_text(kb) == cj.kb_system_text(dict(reversed(list(kb.items()))))
    assert "é" in cj.kb_system_text(kb)
