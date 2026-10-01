"""
One way to ask Claude for JSON, used by every call in the bot.

Structured outputs (`output_config.format` with a JSON schema) make the API constrain Claude's
reply to the schema, so it always parses: no stripping ``` fences, no fallback parsing, no
silently empty dicts when a reply is malformed. A schema also lets us fix things like "every
answer key must be one of the form's question keys" (an enum) instead of checking afterwards.

What structured outputs can't express (see the Claude docs, "JSON Schema limitations"):
numeric ranges, string lengths, and objects with arbitrary keys (every object needs
additionalProperties: false). Ranges are still checked in code where they matter.
"""

import json
import logging
import os

# Writing and judgment calls (tailoring, cover letters, form answers).
# CLAUDE_DRAFT_MODEL in the environment overrides it (used to compare models side by side).
# Sonnet 5.5 chosen 2026-10-01 after a side-by-side on the Waymo packet: same (correct) form
# answers, a more concrete and less inflated cover letter, faster and cheaper per token.
DRAFT_MODEL = os.getenv("CLAUDE_DRAFT_MODEL", "claude-sonnet-5-5")
# If the draft model declines a request (stop_reason "refusal"), retry once on this one
FALLBACK_MODEL = "claude-sonnet-4-6"
# High-volume yes/no scoring
SCORE_MODEL = "claude-haiku-4-5-20251001"


class ClaudeJSONError(Exception):
    """Claude didn't return usable JSON (refusal, or the reply was cut off by max_tokens)."""


def obj(properties, required=None):
    """JSON-schema object: every listed property required unless `required` says otherwise."""
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def arr(items):
    return {"type": "array", "items": items}


STR = {"type": "string"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}


def sampling(model, temperature):
    """Sampling settings for a model. Claude Sonnet 5.x rejects non-default temperature
    (400), so it's only sent to models that accept it."""
    if temperature is None or model.startswith(("claude-sonnet-5", "claude-opus-5", "claude-fable")):
        return {}
    return {"temperature": temperature}


def cached_system(text):
    """A system prompt marked for prompt caching. Calls that share the exact same text
    (e.g. the candidate knowledge base) within 5 minutes pay ~10% for it after the first.
    Too-short text is silently not cached, which is harmless."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def ask_json(client, prompt, schema, *, model=DRAFT_MODEL, max_tokens=16000, system=None,
             temperature=None):
    """Send one prompt, get back a dict guaranteed to match `schema`.
    Raises ClaudeJSONError on a refusal or a reply cut off by max_tokens."""
    params = {
        "model": model, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
        **sampling(model, temperature),
    }
    if system:
        params["system"] = system
    resp = client.messages.create(**params)
    if resp.stop_reason == "refusal" and model != FALLBACK_MODEL and model != SCORE_MODEL:
        logging.warning(f"{model} declined; retrying on {FALLBACK_MODEL}")
        return ask_json(client, prompt, schema, model=FALLBACK_MODEL, max_tokens=max_tokens,
                        system=system, temperature=temperature)
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise ClaudeJSONError(f"stop_reason={resp.stop_reason}")
    usage = getattr(resp, "usage", None)
    if usage is not None and getattr(usage, "cache_read_input_tokens", 0):
        logging.info(f"Prompt cache hit: {usage.cache_read_input_tokens} tokens")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def kb_system_text(kb):
    """The candidate knowledge base as one system block. Built the same way everywhere
    (tailoring passes and form answers) so its text is byte-identical and the prompt cache
    is shared across all the calls that build one packet."""
    import yaml
    return ("CANDIDATE KNOWLEDGE BASE: the only source of facts about the candidate. Never state "
            "anything about them that isn't here.\n\n"
            + yaml.dump(kb, default_flow_style=False, allow_unicode=True, sort_keys=True))
