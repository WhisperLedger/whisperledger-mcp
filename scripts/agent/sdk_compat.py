"""Anthropic SDK compat shim for LiteLLM gateway.

The Jupiter LiteLLM gateway currently returns SSE-formatted streaming
responses even when the client requests stream=False. The Anthropic SDK
does not attempt to parse these and hands back a raw str, causing every
resp.content / resp.usage access to raise AttributeError.

This module monkey-patches the SDK's messages endpoint so that any str
response is transparently parsed from SSE into a Message object.

Import this module ONCE at process start (before any Anthropic client is
constructed) and every messages.create() call org-wide gets the fix.
"""
import json
from typing import Any

try:
    import anthropic
    from anthropic.types import Message
    from anthropic.resources.messages import Messages
except ImportError:
    raise

_PATCHED_FLAG = "_jupiter_sse_compat_installed"


def _parse_sse_to_message_dict(sse_text: str) -> dict:
    """Reconstruct an Anthropic Message dict from an SSE stream body.

    The SSE stream shape:
      event: message_start
      data: {"type":"message_start", "message":{...header w/ empty content...}}

      event: content_block_start
      data: {"type":"content_block_start", "index":0, "content_block":{...}}

      event: content_block_delta
      data: {"type":"content_block_delta", "index":0, "delta":{...}}

      event: content_block_stop
      data: {"type":"content_block_stop", "index":0}

      event: message_delta
      data: {"type":"message_delta", "delta":{"stop_reason":"..."}, "usage":{...}}

      event: message_stop
      data: {"type":"message_stop"}

    We reconstruct by tracking header + accumulating content blocks +
    merging final stop_reason/usage.
    """
    events = []
    for block in sse_text.strip().split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass

    if not events:
        raise ValueError("no SSE events found in response body")

    msg = None
    content_blocks: list[dict] = []

    for evt in events:
        et = evt.get("type")

        if et == "message_start":
            msg = dict(evt.get("message", {}))
            content_blocks = list(msg.get("content", []))

        elif et == "content_block_start":
            idx = evt.get("index", len(content_blocks))
            block = dict(evt.get("content_block", {}))
            while len(content_blocks) <= idx:
                content_blocks.append({})
            content_blocks[idx] = block

        elif et == "content_block_delta":
            idx = evt.get("index", 0)
            if idx >= len(content_blocks):
                continue
            delta = evt.get("delta", {})
            dt = delta.get("type")
            if dt == "text_delta":
                content_blocks[idx].setdefault("text", "")
                content_blocks[idx]["text"] += delta.get("text", "")
            elif dt == "input_json_delta":
                content_blocks[idx].setdefault("_partial_json", "")
                content_blocks[idx]["_partial_json"] += delta.get("partial_json", "")
            elif dt == "thinking_delta":
                content_blocks[idx].setdefault("thinking", "")
                content_blocks[idx]["thinking"] += delta.get("thinking", "")

        elif et == "content_block_stop":
            idx = evt.get("index", 0)
            if idx >= len(content_blocks):
                continue
            if "_partial_json" in content_blocks[idx]:
                partial = content_blocks[idx].pop("_partial_json")
                try:
                    content_blocks[idx]["input"] = json.loads(partial) if partial else {}
                except json.JSONDecodeError:
                    content_blocks[idx]["input"] = {}

        elif et == "message_delta":
            if msg is not None:
                delta = evt.get("delta", {})
                if "stop_reason" in delta:
                    msg["stop_reason"] = delta["stop_reason"]
                if "stop_sequence" in delta:
                    msg["stop_sequence"] = delta["stop_sequence"]
                usage = evt.get("usage")
                if usage:
                    existing = msg.get("usage") or {}
                    msg["usage"] = {**existing, **usage}

        # message_stop is a terminator; nothing to accumulate

    if msg is None:
        raise ValueError("SSE stream did not contain message_start")

    # Clean up any transient scratch fields on content blocks
    for cb in content_blocks:
        cb.pop("_partial_json", None)
    msg["content"] = content_blocks

    # Ensure required fields
    msg.setdefault("stop_reason", "end_turn")
    msg.setdefault("stop_sequence", None)
    msg.setdefault("usage", {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    return msg


def _wrap_create(original_create):
    def create(self, *args, **kwargs):
        result = original_create(self, *args, **kwargs)
        if isinstance(result, str):
            # SSE from LiteLLM gateway on a non-streaming request — reconstruct
            msg_dict = _parse_sse_to_message_dict(result)
            return Message.model_validate(msg_dict)
        return result
    return create


def install() -> bool:
    """Install the monkey-patch. Idempotent."""
    if getattr(Messages, _PATCHED_FLAG, False):
        return False
    original = Messages.create
    Messages.create = _wrap_create(original)
    setattr(Messages, _PATCHED_FLAG, True)
    return True


# Auto-install on import — one-liner integration.
install()
