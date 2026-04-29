import json
import logging
from typing import AsyncGenerator

import httpx

from .config import get_logger
from .models import ClaudeRequest, OpenAIRequest

logger = get_logger("proxy")


def _openai_to_claude_request(req: OpenAIRequest) -> ClaudeRequest:
    """Convert OpenAI chat completion request to Claude format."""
    # Convert messages
    messages = []
    system = None
    for msg in req.messages:
        if msg.role == "system":
            system = msg.content
        else:
            messages.append({"role": msg.role, "content": msg.content or ""})
    
    # Convert stop to stop_sequences
    stop_sequences = None
    if req.stop:
        if isinstance(req.stop, str):
            stop_sequences = [req.stop]
        else:
            stop_sequences = req.stop
    
    return ClaudeRequest(
        model=req.model,
        messages=messages,
        max_tokens=req.max_tokens or 1024,
        temperature=req.temperature,
        stream=req.stream,
        system=system,
        stop_sequences=stop_sequences,
        top_p=req.top_p,
        tools=req.tools,
        tool_choice=req.tool_choice,
        reasoning_effort=req.reasoning_effort,
    )


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


def _convert_tools_to_claude_format(tools):
    """Convert OpenAI format tools to Claude format.
    
    OpenAI format: {"type": "function", "function": {"name": "...", "parameters": {...}}}
    Claude format: {"name": "...", "description": "...", "input_schema": {...}}
    """
    if not tools:
        return tools
    
    claude_tools = []
    for tool in tools:
        if isinstance(tool, dict):
            # Check if it's OpenAI format (has type: "function" and function key)
            if tool.get("type") == "function" and "function" in tool:
                func = tool["function"]
                claude_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {})
                })
            # Check if it's already Claude format (has name and input_schema)
            elif "name" in tool and "input_schema" in tool:
                claude_tools.append(tool)
            # Otherwise pass through as-is
            else:
                claude_tools.append(tool)
        else:
            claude_tools.append(tool)
    
    return claude_tools


def _to_openai_tool_format(tool: dict) -> dict:
    """Convert Claude-style tool to OpenAI tool format."""
    # Claude format: {name, description, input_schema}
    # OpenAI format: {type: "function", function: {name, description, parameters}}
    if tool.get("type") == "function" and "function" in tool:
        # Already in OpenAI format
        return tool
    # Convert from Claude format
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        },
    }


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
        # Convert OpenAI format tools to Claude format if needed
        tools = _convert_to_dict(req.tools)
        body["tools"] = _convert_tools_to_claude_format(tools)
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)
    # Handle reasoning_effort: support both {"reasoning_effort": "high"} and {"output_config": {"effort": "high"}}
    if req.output_config:
        body["output_config"] = req.output_config
    elif req.reasoning_effort:
        body["output_config"] = {"effort": req.reasoning_effort}

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            logger.error(f"Upstream Claude API Error: {resp.status_code}, Response: {resp.text}")
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
        # Convert OpenAI format tools to Claude format if needed
        tools = _convert_to_dict(req.tools)
        body["tools"] = _convert_tools_to_claude_format(tools)
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)
    # Handle reasoning_effort: support both {"reasoning_effort": "high"} and {"output_config": {"effort": "high"}}
    if req.output_config:
        body["output_config"] = req.output_config
    elif req.reasoning_effort:
        body["output_config"] = {"effort": req.reasoning_effort}

    async with httpx.AsyncClient(timeout=120.0) as client:
        logger.debug(f"Claude stream request: {body}")
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                logger.debug(f"Claude stream line: {line}")
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:]  # Remove "data:" prefix
                if data == "[DONE]":
                    break
                logger.debug(f"Yielding data: {data[:50]}...")
                yield f"data: {data}\n\n"


# ── OpenAI-compatible provider ───────────────────────────────────

async def call_openai_passthrough(req: OpenAIRequest, upstream: dict) -> dict:
    """Call OpenAI upstream and return response as-is (passthrough mode)."""
    url = f"{upstream['base_url']}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "content-type": "application/json",
    }
    body = {
        "model": upstream["upstream_model"],
        "messages": [m.model_dump() if hasattr(m, "model_dump") else m for m in req.messages],
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    }
    if req.top_p != 1.0:
        body["top_p"] = req.top_p
    if req.stop:
        body["stop"] = req.stop if isinstance(req.stop, list) else [req.stop]
    if req.tools:
        body["tools"] = req.tools
    if req.tool_choice:
        body["tool_choice"] = req.tool_choice
    if req.reasoning_effort:
        body["reasoning_effort"] = req.reasoning_effort

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            logger.error(f"Upstream OpenAI API Error: {resp.status_code}, Response: {resp.text}")
        resp.raise_for_status()
        return resp.json()


async def stream_openai_passthrough(req: OpenAIRequest, upstream: dict) -> AsyncGenerator[str, None]:
    """Stream from OpenAI upstream and yield as-is (passthrough mode)."""
    url = f"{upstream['base_url']}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "content-type": "application/json",
    }
    body = {
        "model": upstream["upstream_model"],
        "messages": [m.model_dump() if hasattr(m, "model_dump") else m for m in req.messages],
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "stream": True,
    }
    if req.top_p != 1.0:
        body["top_p"] = req.top_p
    if req.stop:
        body["stop"] = req.stop if isinstance(req.stop, list) else [req.stop]
    if req.tools:
        body["tools"] = req.tools
    if req.tool_choice:
        body["tool_choice"] = req.tool_choice
    if req.reasoning_effort:
        body["reasoning_effort"] = req.reasoning_effort

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line and line.startswith("data: "):
                    yield f"data: {line[6:]}\n\n"


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
        # Convert tools to OpenAI format: {type: "function", function: {name, description, parameters}}
        tools = _convert_to_dict(req.tools)
        body["tools"] = [_to_openai_tool_format(t) for t in tools]
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)
    # Handle reasoning_effort: support both {"reasoning_effort": "high"} and {"output_config": {"effort": "high"}}
    # For OpenAI, convert output_config.effort to reasoning_effort
    if req.output_config and "effort" in req.output_config:
        body["reasoning_effort"] = req.output_config["effort"]
    elif req.reasoning_effort:
        body["reasoning_effort"] = req.reasoning_effort

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            logger.error(f"Upstream OpenAI API Error: {resp.status_code}, Response: {resp.text}")
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
        # Convert tools to OpenAI format: {type: "function", function: {name, description, parameters}}
        tools = _convert_to_dict(req.tools)
        body["tools"] = [_to_openai_tool_format(t) for t in tools]
    if req.tool_choice:
        body["tool_choice"] = _convert_to_dict(req.tool_choice)
    # Handle reasoning_effort: support both {"reasoning_effort": "high"} and {"output_config": {"effort": "high"}}
    # For OpenAI, convert output_config.effort to reasoning_effort
    if req.output_config and "effort" in req.output_config:
        body["reasoning_effort"] = req.output_config["effort"]
    elif req.reasoning_effort:
        body["reasoning_effort"] = req.reasoning_effort

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
            # Filter out 'thinking' type content - it's intermediate and shouldn't be sent upstream
            content = []
            for c in msg.content:
                c_dict = _convert_to_dict(c)
                if isinstance(c_dict, dict) and c_dict.get("type") == "thinking":
                    continue  # Skip thinking blocks
                content.append(c_dict)
            # If all content was filtered out, skip this message
            if not content:
                continue
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
    message = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")
    stop_map = {"stop": "end_turn", "length": "max_tokens", "content_filter": "stop_sequence", "tool_calls": "tool_use"}
    usage = resp.get("usage", {})
    
    content = []

    # Handle reasoning/thinking content if present
    # Check both "reasoning_content" (OpenAI standard) and "reasoning" (alternative)
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})

    # Handle text content if present
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})
    
    # Handle tool_calls if present
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        if tc.get("type") == "function":
            func = tc.get("function", {})
            # Parse arguments if it's a JSON string
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "input": args,
            })
    
    # Fallback: if no content at all, add empty text
    if not content:
        content.append({"type": "text", "text": ""})
    
    return {
        "id": resp.get("id", ""),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_map.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _stream_openai_to_claude(resp, model: str) -> AsyncGenerator[str, None]:
    """Convert OpenAI stream response to Claude stream format.
    
    Simulates full Claude streaming format:
    message_start -> content_block_start(thinking) -> content_block_delta(thinking) -> content_block_stop
                  -> content_block_start(text) -> content_block_delta(text) -> content_block_stop
                  -> message_delta -> message_stop
    """
    import time
    import uuid
    
    # Generate message ID
    message_id = f"msg_{uuid.uuid4()}"
    created_time = int(time.time())
    
    # Track state
    message_started = False
    thinking_started = False
    text_started = False
    input_tokens = 0
    output_tokens = 0
    
    async for line in resp.aiter_lines():
        if not line or not line.startswith("data: "):
            continue

        data = line[6:]
        if data == "[DONE]":
            # Send content_block_stop for any open blocks
            if text_started:
                event = {"type": "content_block_stop", "index": 1}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            elif thinking_started:
                event = {"type": "content_block_stop", "index": 0}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            
            # Send message_delta and message_stop
            yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"end_turn","stop_sequence":null}},"usage":{{"input_tokens":{input_tokens},"output_tokens":{output_tokens}}}}}\n\n'
            yield 'data: {"type":"message_stop"}\n\n'
            break

        try:
            chunk = json.loads(data)
            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            
            # Send message_start on first content
            if not message_started and (delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")):
                message_started = True
                event = {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}
                    }
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            
            # Extract reasoning_content first (if present) - convert to thinking_delta
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                if not thinking_started:
                    # Send content_block_start for thinking
                    event = {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""}
                    }
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    thinking_started = True
                
                event = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # Extract content text
            text = delta.get("content")
            if text:
                if not text_started:
                    # Close thinking block if open
                    if thinking_started:
                        event = {"type": "content_block_stop", "index": 0}
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    
                    # Send content_block_start for text
                    event = {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {"type": "text", "text": ""}
                    }
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    text_started = True
                
                event = {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": text},
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # Check for finish reason - collect usage info
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                usage = chunk.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                # message_delta and message_stop will be sent when [DONE] is received
        except json.JSONDecodeError:
            continue


def _claude_to_openai(resp: dict) -> dict:
    """Convert Claude response to OpenAI chat completion format."""
    import time

    # Extract text, thinking and tool_calls from content
    text = ""
    thinking = ""
    tool_calls = []

    for block in resp.get("content", []):
        block_type = block.get("type", "")
        # Handle both "text" and "text_delta" types
        if block_type in ("text", "text_delta"):
            text += block.get("text", "")
        elif block_type == "thinking":
            thinking += block.get("thinking", "")
        elif block_type == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    # Map stop_reason
    stop_reason = resp.get("stop_reason", "end_turn")
    finish_map = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }

    # Build message
    message = {"role": "assistant"}
    if text:
        message["content"] = text
    if thinking:
        message["reasoning_content"] = thinking
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = resp.get("usage", {})

    return {
        "id": resp.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_map.get(stop_reason, "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }
