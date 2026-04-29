import json
import logging
from typing import AsyncGenerator

import httpx

from .config import get_logger
from .models import ClaudeRequest, OpenAIRequest

logger = get_logger("proxy")


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


# ── Claude provider (passthrough only) ──────────────────────────

async def call_claude_passthrough(req: ClaudeRequest, upstream: dict) -> dict:
    """Call Claude upstream and return response as-is (full passthrough).

    All request fields are passed through unchanged to the upstream API.
    """
    url = f"{upstream['base_url']}/v1/messages"
    headers = {
        "x-api-key": upstream["api_key"],
        "anthropic-version": upstream["api_version"],
        "content-type": "application/json",
    }
    # Build request body from all model_dump fields (including extra fields)
    body = req.model_dump(exclude_none=True, exclude={"model"})
    body["model"] = upstream["upstream_model"]

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            logger.error(f"Upstream Claude API Error: {resp.status_code}, Response: {resp.text}")
        resp.raise_for_status()
        return resp.json()


async def stream_claude_passthrough(req: ClaudeRequest, upstream: dict) -> AsyncGenerator[str, None]:
    """Stream from Claude upstream and yield as-is (full passthrough).

    All request fields are passed through unchanged to the upstream API.
    All response lines are yielded exactly as received from upstream.
    """
    url = f"{upstream['base_url']}/v1/messages"
    headers = {
        "x-api-key": upstream["api_key"],
        "anthropic-version": upstream["api_version"],
        "content-type": "application/json",
    }
    # Build request body from all model_dump fields (including extra fields)
    body = req.model_dump(exclude_none=True, exclude={"model"})
    body["model"] = upstream["upstream_model"]
    body["stream"] = True

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                # Yield every line exactly as received (including data:, [DONE], etc.)
                yield line + "\n"


# ── OpenAI provider (passthrough only) ──────────────────────────

async def call_openai_passthrough(req: OpenAIRequest, upstream: dict) -> dict:
    """Call OpenAI upstream and return response as-is (full passthrough).

    All request fields are passed through unchanged to the upstream API.
    """
    url = f"{upstream['base_url']}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "content-type": "application/json",
    }
    # Build request body from all model_dump fields (including extra fields)
    body = req.model_dump(exclude_none=True, exclude={"model"})
    body["model"] = upstream["upstream_model"]

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            logger.error(f"Upstream OpenAI API Error: {resp.status_code}, Response: {resp.text}")
        resp.raise_for_status()
        return resp.json()


async def stream_openai_passthrough(req: OpenAIRequest, upstream: dict) -> AsyncGenerator[str, None]:
    """Stream from OpenAI upstream and yield as-is (full passthrough).

    All request fields are passed through unchanged to the upstream API.
    All response lines are yielded exactly as received from upstream.
    """
    url = f"{upstream['base_url']}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "content-type": "application/json",
    }
    # Build request body from all model_dump fields (including extra fields)
    body = req.model_dump(exclude_none=True, exclude={"model"})
    body["model"] = upstream["upstream_model"]
    body["stream"] = True

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                # Yield every line exactly as received (including data:, [DONE], etc.)
                yield line + "\n"
