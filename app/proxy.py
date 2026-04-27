import json
from typing import AsyncGenerator

import httpx

from .models import ClaudeRequest


# ── Helper functions ────────────────────────────────────────────

def _convert_to_dict(obj):
    """Convert Pydantic model or list of models to dict/list."""
    if obj is None:
        return None
    if isinstance(obj, list):
        return [item.model_dump() if hasattr(item, "model_dump") else item for item in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


# ── Claude provider ──────────────────────────────────────────────

async def call_claude(req: ClaudeRequest, upstream: dict) -> dict:
    url = f"{upstream['base_url']}/v1/messages"
    headers = {
        "x-api-key": upstream["api_key"],
        "anthropic-version": upstream["api_version"],
        "content-type": "application/json",
    }
    body = {
        "model": upstream["upstream_model"],
        "messages": _to_claude_messages(req),
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    }
    # Extract system text if present (handle both string and array format)
    system_text = _extract_system(req.system)
    if system_text:
        body["system"] = system_text
    if req.top_p != 1.0:
        body["top_p"] = req.top_p
    if req.top_k:
        body["top_k"] = req.top_k
    if req.stop_sequences:
        body["stop_sequences"] = req.stop_sequences
    if req.tools:
        body["tools"] = _convert_to_dict(req.tools)
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()


async def stream_claude(req: ClaudeRequest, upstream: dict) -> AsyncGenerator[str, None]:
    url = f"{upstream['base_url']}/v1/messages"
    headers = {
        "x-api-key": upstream["api_key"],
        "anthropic-version": upstream["api_version"],
        "content-type": "application/json",
    }
    body = {
        "model": upstream["upstream_model"],
        "messages": _to_claude_messages(req),
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "stream": True,
    }
    # Extract system text if present (handle both string and array format)
    system_text = _extract_system(req.system)
    if system_text:
        body["system"] = system_text
    if req.top_p != 1.0:
        body["top_p"] = req.top_p
    if req.top_k:
        body["top_k"] = req.top_k
    if req.stop_sequences:
        body["stop_sequences"] = req.stop_sequences
    if req.tools:
        body["tools"] = _convert_to_dict(req.tools)
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                yield f"data: {data}\n\n"


# ── OpenAI-compatible provider ───────────────────────────────────

async def call_openai(req: ClaudeRequest, upstream: dict) -> dict:
    url = f"{upstream['base_url']}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "content-type": "application/json",
    }
    body = {
        "model": upstream["upstream_model"],
        "messages": _to_openai_messages(req),
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    }
    if req.top_p != 1.0:
        body["top_p"] = req.top_p
    if req.stop_sequences:
        body["stop"] = req.stop_sequences
    if req.tools:
        body["tools"] = _convert_to_dict(req.tools)
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return _openai_to_claude(resp.json(), upstream["upstream_model"])


async def stream_openai(req: ClaudeRequest, upstream: dict) -> AsyncGenerator[str, None]:
    url = f"{upstream['base_url']}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "content-type": "application/json",
    }
    body = {
        "model": upstream["upstream_model"],
        "messages": _to_openai_messages(req),
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "stream": True,
    }
    if req.top_p != 1.0:
        body["top_p"] = req.top_p
    if req.stop_sequences:
        body["stop"] = req.stop_sequences
    if req.tools:
        body["tools"] = _convert_to_dict(req.tools)
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for chunk in _stream_openai_to_claude(resp, upstream["upstream_model"]):
                yield chunk


# ── Format converters ────────────────────────────────────────────

def _extract_content(content) -> str:
    """Extract text from content which may be string or [{type, text/thinking}] array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "text":
                    texts.append(item.get("text", ""))
                elif item_type == "thinking":
                    texts.append(item.get("thinking", ""))
                elif item_type == "tool_result" and isinstance(item.get("content"), str):
                    texts.append(item.get("content", ""))
        return "".join(texts)
    return str(content)


def _extract_system(system) -> str | None:
    """Extract system text from system which may be string or [{type, text, cache_control}] array."""
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        texts = []
        for item in system:
            if isinstance(item, dict):
                texts.append(item.get("text", ""))
        return "".join(texts)
    return str(system)


def _to_claude_messages(req: ClaudeRequest) -> list[dict]:
    messages = []
    for msg in req.messages:
        if msg.role == "system":
            continue  # system handled separately
        # Convert content to dict format for JSON serialization
        if isinstance(msg.content, list):
            content = [_convert_to_dict(c) for c in msg.content]
        else:
            content = msg.content
        messages.append({"role": msg.role, "content": content})
    return messages


def _to_openai_messages(req: ClaudeRequest) -> list[dict]:
    messages = []
    for msg in req.messages:
        # OpenAI expects string content, extract from Claude's array format if needed
        content = _extract_content(msg.content)
        messages.append({"role": msg.role, "content": content})
    
    # Add system message if present
    if req.system:
        system_text = _extract_system(req.system)
        if system_text:
            messages.insert(0, {"role": "system", "content": system_text})
    
    return messages


def _openai_to_claude(resp: dict, model: str) -> dict:
    choice = resp["choices"][0]
    text = choice.get("message", {}).get("content", "")
    finish = choice.get("finish_reason", "stop")
    stop_map = {"stop": "end_turn", "length": "max_tokens", "content_filter": "stop_sequence"}
    usage = resp.get("usage", {})
    return {
        "id": resp.get("id", ""),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": stop_map.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _stream_openai_to_claude(resp, model: str) -> AsyncGenerator[str, None]:
    async for line in resp.aiter_lines():
        if not line or not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"end_turn","stop_sequence":null}},"usage":{{"output_tokens":0}}}}\n\n'
            yield "data: [DONE]\n\n"
            break

        try:
            chunk = json.loads(data)
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            text = delta.get("content", "")
            if text:
                event = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
                yield f"data: {json.dumps(event)}\n\n"
        except json.JSONDecodeError:
            continue
